"""`faktura-eval-extract`: run extraction over a synthetic dataset and report accuracy.

Raw model outputs are cached per invoice under <run>/raw/, so an interrupted run resumes
without paying twice, and scoring can be re-run offline.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from faktura.evals.extraction_scoring import all_wrong, score_fields
from faktura.extract.client import AzureResponsesClient, VisionClient
from faktura.extract.pdf import render_first_page_png
from faktura.extract.preprocess import preprocess
from faktura.extract.schema import ExtractedInvoice, FieldName
from faktura.schemas import InvoiceLabel

# USD per 1M tokens: input, cached input, output. List prices; check the Azure price sheet.
DEFAULT_PRICES = {"gpt-6-luna": (0.10, 0.01, 0.50)}
SLICE_KEYS = ("level", "layout", "money_style", "date_style", "lang", "stamp", "scanned")


@dataclass
class CaseResult:
    invoice_id: str
    scores: dict[FieldName, bool]
    flagged: set[FieldName] = field(default_factory=set)
    render: dict = field(default_factory=dict)
    error: str | None = None
    latency_s: float = 0.0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


def load_labels(data_dir: Path) -> list[InvoiceLabel]:
    text = (data_dir / "labels.jsonl").read_text(encoding="utf-8")
    return [InvoiceLabel.model_validate_json(line) for line in text.splitlines() if line]


def select(labels: list[InvoiceLabel], limit: int | None, seed: int) -> list[InvoiceLabel]:
    if limit is None or limit >= len(labels):
        return labels
    return sorted(random.Random(seed).sample(labels, limit), key=lambda lb: lb.invoice_id)


def run_one(
    client: VisionClient,
    label: InvoiceLabel,
    data_dir: Path,
    raw_dir: Path,
    use_preprocess: bool = False,
    reuse_dir: Path | None = None,
) -> dict:
    cache = raw_dir / f"{label.invoice_id}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    png = render_first_page_png(data_dir / "invoices" / f"{label.invoice_id}.pdf")
    pre_meta: dict = {}
    if use_preprocess:
        png, pre_meta = preprocess(png)
        # Image unchanged (not a photo): reuse an earlier reading of the identical input
        # instead of paying for the same call again.
        old = reuse_dir / "raw" / f"{label.invoice_id}.json" if reuse_dir else None
        if not pre_meta["photo"] and old is not None and old.exists():
            record = json.loads(old.read_text(encoding="utf-8")) | {
                "preprocess": pre_meta,
                "reused": True,
            }
            cache.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return record
    try:
        res = client.extract(png)
        record = {
            "invoice_id": label.invoice_id,
            "model": res.model,
            "extracted": res.invoice.model_dump(mode="json"),
            "latency_s": res.latency_s,
            "usage": vars(res.usage),
            "preprocess": pre_meta,
        }
    except Exception as e:  # recorded and scored as wrong, never silently dropped
        record = {"invoice_id": label.invoice_id, "error": f"{type(e).__name__}: {e}"}
    if "error" not in record:  # failures are retried on the next run
        cache.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def score_record(record: dict, label: InvoiceLabel) -> CaseResult:
    if "error" in record:
        return CaseResult(label.invoice_id, all_wrong(), render=label.render, error=record["error"])
    ext = ExtractedInvoice.model_validate(record["extracted"])
    usage = record.get("usage", {})
    return CaseResult(
        invoice_id=label.invoice_id,
        scores=score_fields(ext, label.invoice),
        flagged=set(ext.uncertain_fields),
        render=label.render,
        latency_s=record.get("latency_s", 0.0),
        input_tokens=usage.get("input_tokens", 0),
        cached_input_tokens=usage.get("cached_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
    )


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def _p95(xs: list[float]) -> float:
    if len(xs) < 2:
        return xs[0] if xs else 0.0
    return statistics.quantiles(xs, n=20, method="inclusive")[18]


def build_report(results: list[CaseResult], model: str) -> dict:
    n = len(results)
    per_field = {f.value: sum(r.scores[f] for r in results) / n if n else 0.0 for f in FieldName}
    exact = sum(all(r.scores.values()) for r in results)

    slices: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        for key in SLICE_KEYS:
            if key in r.render:
                slices[f"{key}={r.render[key]}"].append(r)
    by_slice = {
        k: {"n": len(v), "invoice_exact": sum(all(r.scores.values()) for r in v) / len(v)}
        for k, v in sorted(slices.items())
    }
    # Field accuracy per degradation level: the "where does it break" curve.
    by_level: dict[str, dict[str, float]] = {}
    for lvl in sorted({r.render["level"] for r in results if "level" in r.render}):
        group = [r for r in results if r.render.get("level") == lvl]
        by_level[str(lvl)] = {
            f.value: sum(r.scores[f] for r in group) / len(group) for f in FieldName
        }

    # Does the model's uncertainty flag catch its own mistakes?
    wrong = flagged_wrong = flagged = 0
    for r in results:
        if r.error:
            continue
        for f, ok in r.scores.items():
            wrong += not ok
            flagged += f in r.flagged
            flagged_wrong += (not ok) and f in r.flagged

    ok_runs = [r for r in results if not r.error]
    lat = [r.latency_s for r in ok_runs]
    tin = sum(r.input_tokens - r.cached_input_tokens for r in ok_runs)
    tcache = sum(r.cached_input_tokens for r in ok_runs)
    tout = sum(r.output_tokens for r in ok_runs)
    price = DEFAULT_PRICES.get(model)
    cost = (tin * price[0] + tcache * price[1] + tout * price[2]) / 1e6 if price else None

    return {
        "model": model,
        "n": n,
        "errors": n - len(ok_runs),
        "invoice_exact": exact / n if n else 0.0,
        "per_field": per_field,
        "by_slice": by_slice,
        "by_level_field": by_level,
        "uncertainty_flags": {
            "field_errors": wrong,
            "errors_flagged": flagged_wrong,
            "flags_raised": flagged,
            "error_recall": flagged_wrong / wrong if wrong else None,
            "flag_precision": flagged_wrong / flagged if flagged else None,
        },
        "latency_s": {
            "p50": statistics.median(lat) if lat else None,
            "p95": _p95(lat) if lat else None,
        },
        "tokens": {"input": tin, "cached_input": tcache, "output": tout},
        "cost_usd_total": cost,
        "cost_usd_per_1000_invoices": cost / len(ok_runs) * 1000 if cost and ok_runs else None,
        "failed": {r.invoice_id: r.error for r in results if r.error},
    }


def format_report(rep: dict) -> str:
    out = [
        f"# Extraction eval: {rep['model']}",
        "",
        f"- invoices: {rep['n']} (errors: {rep['errors']})",
        f"- invoice exact match: {100 * rep['invoice_exact']:.1f}%",
        "",
        "| field | accuracy |",
        "|---|---|",
    ]
    out += [f"| {k} | {100 * v:.1f}% |" for k, v in rep["per_field"].items()]
    out += ["", "| slice | n | invoice exact |", "|---|---|---|"]
    out += [
        f"| {k} | {v['n']} | {100 * v['invoice_exact']:.1f}% |" for k, v in rep["by_slice"].items()
    ]
    if rep["by_level_field"]:
        levels = list(rep["by_level_field"])
        out += ["", "| field | " + " | ".join(f"level {k}" for k in levels) + " |"]
        out += ["|---|" + "---|" * len(levels)]
        for f in FieldName:
            cells = " | ".join(f"{100 * rep['by_level_field'][k][f.value]:.1f}%" for k in levels)
            out.append(f"| {f.value} | {cells} |")
    uf = rep["uncertainty_flags"]
    out += [
        "",
        f"- field errors: {uf['field_errors']}, of which flagged uncertain: "
        f"{_pct(uf['errors_flagged'], uf['field_errors'])}; "
        f"flags that were real errors: {_pct(uf['errors_flagged'], uf['flags_raised'])}",
        f"- latency p50/p95: {rep['latency_s']['p50']} / {rep['latency_s']['p95']} s",
        f"- tokens: {rep['tokens']}",
        f"- est. cost per 1000 invoices: {rep['cost_usd_per_1000_invoices']} USD (list price)",
    ]
    if rep["failed"]:
        out += ["", "Failed calls:"] + [f"- {k}: {v}" for k, v in rep["failed"].items()]
    return "\n".join(out)


def evaluate(
    client: VisionClient,
    data_dir: Path,
    run_dir: Path,
    limit: int | None = None,
    seed: int = 0,
    workers: int = 4,
    use_preprocess: bool = False,
    reuse_dir: Path | None = None,
    exclude_dir: Path | None = None,
) -> dict:
    labels = load_labels(data_dir)
    if exclude_dir is not None:
        held_out = {p.stem for p in (exclude_dir / "raw").glob("*.json")}
        labels = [lb for lb in labels if lb.invoice_id not in held_out]
    labels = select(labels, limit, seed)
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    by_id = {lb.invoice_id: lb for lb in labels}

    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(run_one, client, lb, data_dir, raw_dir, use_preprocess, reuse_dir)
            for lb in labels
        ]
        for fut in as_completed(futures):
            records.append(fut.result())

    results = sorted(
        (score_record(r, by_id[r["invoice_id"]]) for r in records), key=lambda r: r.invoice_id
    )
    report = build_report(results, client.model)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(format_report(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faktura-eval-extract")
    parser.add_argument("--data", type=Path, default=Path("data/synth/v1"))
    parser.add_argument("--run", type=Path, required=True, help="output dir, e.g. runs/luna-v1")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--preprocess", action="store_true", help="clean up phone photos")
    parser.add_argument(
        "--reuse",
        type=Path,
        default=None,
        help="earlier run whose readings are reused for unchanged images",
    )
    parser.add_argument(
        "--exclude", type=Path, default=None, help="run dir whose invoices are left out (dev set)"
    )
    args = parser.parse_args(argv)

    load_dotenv()
    client = AzureResponsesClient.from_env()
    rep = evaluate(
        client,
        args.data,
        args.run,
        args.limit,
        args.seed,
        args.workers,
        args.preprocess,
        args.reuse,
        args.exclude,
    )
    print(format_report(rep))
    return 1 if rep["errors"] == rep["n"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
