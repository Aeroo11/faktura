"""`faktura-eval-reread`: apply the targeted second read to a finished extraction run.

Reads the first-pass outputs of --base, runs the validators, re-reads only flagged fields,
and writes merged records to --out/raw in the same format as an extraction run. The
existing extraction and validation reports can then be run on --out unchanged.

Also reports, per field: fixed (wrong -> right), broken (right -> wrong), and whether the
flags of genuine anomalies survived the second read.
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from faktura.evals.extraction import build_report, format_report, load_labels, score_record
from faktura.evals.extraction_scoring import score_fields
from faktura.evals.validation import ANOMALY_FIELDS, history_excluding
from faktura.extract.client import AzureResponsesClient
from faktura.extract.pdf import render_first_page_png
from faktura.extract.preprocess import preprocess
from faktura.extract.reread import reread
from faktura.extract.schema import ExtractedInvoice, FieldName
from faktura.learn.misread import MisreadModel
from faktura.master import MasterData
from faktura.synth.config import load_config
from faktura.validate import suspect_fields, validate

REREAD_DPI = 300


def process_one(
    client,
    base: dict,
    lb,
    md_snapshot: MasterData,
    vat_bps: int,
    data_dir: Path,
    out_raw: Path,
    tiles: bool = False,
    use_preprocess: bool = False,
    misread_hint: str | None = None,
) -> dict:
    cache = out_raw / f"{lb.invoice_id}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    ext = ExtractedInvoice.model_validate(base["extracted"])
    issues = validate(ext, md_snapshot, vat_bps)
    rec = dict(base) | {"reread_fields": [], "first_pass": base["extracted"]}
    if issues:
        png = render_first_page_png(data_dir / "invoices" / f"{lb.invoice_id}.pdf", REREAD_DPI)
        if use_preprocess:
            png, _ = preprocess(png)
        try:
            merged, fields, usage, latency = reread(
                client, png, ext, issues, tiles=tiles, misread_hint=misread_hint
            )
        except Exception as e:  # keep the first reading; record why
            rec["reread_error"] = f"{type(e).__name__}: {e}"
            return rec  # not cached, so it is retried next time
        rec["extracted"] = merged.model_dump(mode="json")
        rec["reread_fields"] = sorted(f.value for f in fields)
        rec["latency_s"] = base.get("latency_s", 0.0) + latency
        rec["usage"] = {k: base["usage"].get(k, 0) + v for k, v in vars(usage).items()}
    cache.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def change_stats(
    records: list[dict], labels: dict, md: MasterData, history: dict, vat_bps: int
) -> dict:
    fixed, broken, still_wrong = Counter(), Counter(), Counter()
    anomaly_flags_total = anomaly_flags_kept = 0
    for rec in records:
        lb = labels[rec["invoice_id"]]
        before = score_fields(ExtractedInvoice.model_validate(rec["first_pass"]), lb.invoice)
        after_ext = ExtractedInvoice.model_validate(rec["extracted"])
        after = score_fields(after_ext, lb.invoice)
        for f in FieldName:
            if not before[f] and after[f]:
                fixed[f.value] += 1
            elif before[f] and not after[f]:
                broken[f.value] += 1
            elif not before[f] and not after[f]:
                still_wrong[f.value] += 1
        expected = set()
        for a in lb.anomalies:
            expected |= ANOMALY_FIELDS[a]
        if expected:
            md.approved_invoice_numbers = history[lb.invoice_id]
            flagged = suspect_fields(validate(after_ext, md, vat_bps))
            anomaly_flags_total += len(expected)
            anomaly_flags_kept += len(expected & flagged)
    return {
        "fixed": dict(fixed),
        "broken": dict(broken),
        "still_wrong": dict(still_wrong),
        "fixed_total": sum(fixed.values()),
        "broken_total": sum(broken.values()),
        "anomaly_fields_flagged_after": f"{anomaly_flags_kept}/{anomaly_flags_total}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faktura-eval-reread")
    parser.add_argument("--data", type=Path, default=Path("data/synth/v2"))
    parser.add_argument("--base", type=Path, required=True, help="first-pass extraction run")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None, help="random subset, for pilots")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--tiles", action="store_true", help="add zoomed page crops")
    parser.add_argument("--preprocess", action="store_true", help="clean up phone photos")
    parser.add_argument(
        "--exclude", type=Path, default=None, help="run dir whose invoices are left out (dev pilot)"
    )
    parser.add_argument(
        "--misread-model", type=Path, default=None, help="add learned misread hints (no values)"
    )
    args = parser.parse_args(argv)

    load_dotenv()
    hint = MisreadModel.load(args.misread_model).hint() if args.misread_model else None
    print("misread hint:", hint)
    client = AzureResponsesClient.from_env()
    vat_bps = load_config(args.data / "config.yaml").vat_bps
    labels = {lb.invoice_id: lb for lb in load_labels(args.data)}
    history = history_excluding(list(labels.values()), MasterData.load(args.data))
    bases = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((args.base / "raw").glob("*.json"))
    ]
    if args.exclude:
        dev = {p.stem for p in (args.exclude / "raw").glob("*.json")}
        bases = [b for b in bases if b["invoice_id"] not in dev]
    if args.limit:
        bases = sorted(
            random.Random(args.seed).sample(bases, args.limit), key=lambda b: b["invoice_id"]
        )
    out_raw = args.out / "raw"
    out_raw.mkdir(parents=True, exist_ok=True)

    def snapshot(invoice_id: str) -> MasterData:
        md = MasterData.load(args.data)  # one per task: approved history differs per invoice
        md.approved_invoice_numbers = history[invoice_id]
        return md

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(
                process_one,
                client,
                b,
                labels[b["invoice_id"]],
                snapshot(b["invoice_id"]),
                vat_bps,
                args.data,
                out_raw,
                args.tiles,
                args.preprocess,
                hint,
            )
            for b in bases
        ]
        records = [f.result() for f in as_completed(futures)]

    results = sorted(
        (score_record(r, labels[r["invoice_id"]]) for r in records), key=lambda r: r.invoice_id
    )
    rep = build_report(results, client.model)
    stats = change_stats(records, labels, MasterData.load(args.data), history, vat_bps)
    rep["reread"] = stats | {
        "misread_hint": hint,
        "invoices_reread": sum(bool(r["reread_fields"]) for r in records),
        "reread_errors": sum("reread_error" in r for r in records),
    }
    (args.out / "report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    text = format_report(rep) + "\n\n## Second read\n\n" + json.dumps(rep["reread"], indent=2)
    (args.out / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
