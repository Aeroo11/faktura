"""`faktura-bench-cord`: zero-shot vision-LLM extraction on the CORD v2 test split.

Scored with Donut's own evaluator (bench/donut_metric.py) against `gt_parse`, so the
numbers are directly comparable with the published Donut result. Comparability caveats
(fine-tuned vs zero-shot, possible pre-training contamination) are listed in the report.
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image
from pydantic import BaseModel, Field

from faktura.bench.donut_metric import JSONParseEvaluator
from faktura.extract.client import AzureResponsesClient

DATASET = "naver-clova-ix/cord-v2"
MAX_SIDE_PX = 2200

# Published baseline, test split of cord-v2. Source: README of github.com/clovaai/donut
# (results table: donut-base-finetuned-cord-v2, score 91.3; test example output:
# TED accuracy 0.9129..., F1 0.8406...).
DONUT_BASELINE = {"model": "Donut (fine-tuned on CORD train)", "ted_accuracy": 0.913, "f1": 0.841}


# Schema follows the cord-v2 `gt_parse` JSON and the official CORD label table.
# Values are strings, copied as printed: the metric compares exact text.
class SubItem(BaseModel):
    nm: str | None = Field(description="name of the sub-menu item (topping, add-on)")
    unitprice: str | None = Field(description="unit price of the sub-menu item")
    cnt: str | None = Field(description="quantity of the sub-menu item")
    price: str | None = Field(description="total price of the sub-menu item")
    etc: str | None = Field(description="other text belonging to the sub-menu item")


class MenuItem(BaseModel):
    nm: str | None = Field(description="name of the menu item")
    num: str | None = Field(description="identification number / code of the item")
    unitprice: str | None = Field(description="unit price")
    cnt: str | None = Field(description="quantity, as printed (e.g. '2', '2x', '1.00')")
    discountprice: str | None = Field(description="discount on this item")
    price: str | None = Field(description="total price of this item line")
    itemsubtotal: str | None = Field(description="price of this item after discount")
    vatyn: str | None = Field(description="marker whether the price includes tax")
    etc: str | None = Field(description="other text on this item line")
    sub: list[SubItem] = Field(description="sub-menu items listed under this item")


class VoidItem(BaseModel):
    nm: str | None = Field(description="name of the voided/cancelled item")
    price: str | None = Field(description="price of the voided item")


class SubTotal(BaseModel):
    subtotal_price: str | None
    discount_price: str | None = Field(description="discount on the whole bill")
    service_price: str | None = Field(description="service charge")
    othersvc_price: str | None = Field(description="added charge other than service")
    tax_price: str | None = Field(description="tax amount")
    etc: str | None = Field(description="other subtotal-area value, e.g. rounding")


class Total(BaseModel):
    total_price: str | None
    total_etc: str | None = Field(description="other total-area value")
    cashprice: str | None = Field(description="amount paid in cash")
    changeprice: str | None = Field(description="change returned")
    creditcardprice: str | None = Field(description="amount paid by credit/debit card")
    emoneyprice: str | None = Field(description="amount paid by e-money or points")
    menutype_cnt: str | None = Field(description="printed count of item types")
    menuqty_cnt: str | None = Field(description="printed total quantity of items")


class CordParse(BaseModel):
    menu: list[MenuItem]
    void_menu: list[VoidItem]
    sub_total: SubTotal
    total: Total


CORD_PROMPT = """\
You parse Indonesian shop and restaurant receipts into a fixed JSON schema.
Rules:
- Copy every value exactly as printed: same characters, case, thousand separators,
  decimals, minus signs and currency text. Never reformat, convert, or compute a value.
- Use null for anything that is not printed. Do not infer missing values.
- One menu entry per purchased item, in printed order. Modifiers, toppings, or add-ons
  printed under an item go in that item's `sub` list.
- Ignore shop name, address, date, cashier, table number, and other header/footer text."""

# p2 was written from the error categories of p1 on the validation split (verbatim-copy
# errors: spacing, thousand separators, dropped symbols, case, and item vs sub-item).
CORD_PROMPT_P2 = """\
You transcribe Indonesian shop and restaurant receipts into a fixed JSON schema.
Every value is scored by exact string match against a character-level transcription,
so you are a copier, not an editor.

Copy characters exactly as printed:
- Keep the printed thousand separator, even if it is not the Indonesian convention:
  "8,000" stays "8,000"; "8.000" stays "8.000". Never switch between "," and ".".
- Keep spaces as printed: "2 x" stays "2 x", "Rp 29,090" stays "Rp 29,090",
  "( L )" stays "( L )". Do not add or remove spaces.
- Keep symbols and prefixes that are part of the value: "@28,000", "@ 12.000", ":9,000",
  "*Rhum", "-5,000", "x2", "Rp". Do not strip them.
- Keep the printed case and spelling, including typos and abbreviations: "ITEMs",
  "BLACKCOFFE", "PKT TELOR/PERKEDEL". Never correct or expand words.
- Use null for anything that is not printed. Never compute, infer, or fill in values.

Structure:
- One menu entry per purchased item line, in printed order.
- Lines printed under an item as add-ons, toppings, options, or modifiers (often indented
  or starting with "-", "*", or "+") are that item's `sub` entries, not new menu items.
- `cnt` is the printed quantity text of that line; `num` is an item code, not a quantity.
- Values in the totals area go to sub_total or total; never copy them into menu items.
- Ignore shop name, address, date, cashier, table number, and other header/footer text."""

# Prompt versions are frozen once a test-split number is reported with them. New versions
# are developed on the validation split only, then run on test once.
PROMPTS: dict[str, str] = {"p1": CORD_PROMPT, "p2": CORD_PROMPT_P2}


def to_gt_like(p: CordParse) -> dict:
    """Pydantic output -> plain dict without nulls or empty containers."""

    def clean(v):
        if isinstance(v, dict):
            out = {k: clean(x) for k, x in v.items()}
            return {k: x for k, x in out.items() if x not in (None, "", [], {})}
        if isinstance(v, list):
            return [c for c in (clean(x) for x in v) if c not in (None, "", [], {})]
        return v

    return clean(p.model_dump())


def prepare_image(raw: bytes) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    if max(img.size) > MAX_SIDE_PX:
        img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


def split_files(split: str) -> list[str]:
    """Local paths of the parquet files of one cord-v2 split (downloaded once, cached)."""
    from huggingface_hub import hf_hub_download, list_repo_files

    names = sorted(f for f in list_repo_files(DATASET, repo_type="dataset") if f"/{split}-" in f)
    if not names:
        raise ValueError(f"no parquet files for split {split!r}")
    return [hf_hub_download(DATASET, f, repo_type="dataset") for f in names]


def load_ground_truth(split: str = "test") -> list[dict]:
    """[{idx, gt}] for one split. Reads only the small ground_truth column."""
    import pyarrow.parquet as pq

    rows: list[str] = []
    for path in split_files(split):
        rows.extend(pq.read_table(path, columns=["ground_truth"]).column(0).to_pylist())
    return [{"idx": i, "gt": json.loads(r)["gt_parse"]} for i, r in enumerate(rows)]


def iter_images(split: str, batch_size: int = 4) -> Iterator[tuple[int, bytes]]:
    """Stream (idx, image bytes) a few rows at a time, so only one batch is in memory."""
    import pyarrow.parquet as pq

    idx = 0
    for path in split_files(split):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_size, columns=["image"]):
            for cell in batch.column(0).to_pylist():
                yield idx, cell["bytes"]
                idx += 1


def run_one(
    client: AzureResponsesClient,
    sample: dict,
    raw_dir: Path,
    prompt: str = CORD_PROMPT,
    detail: str | None = None,
) -> dict:
    cache = raw_dir / f"cord-{sample['idx']:03d}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    image, mime = prepare_image(sample["image"])
    try:
        parsed, usage, latency = client.parse_image(
            image, mime, prompt, "Parse this receipt.", CordParse, detail=detail
        )
        rec = {
            "idx": sample["idx"],
            "pred": to_gt_like(parsed),
            "latency_s": latency,
            "usage": vars(usage),
        }
        cache.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # scored as an empty prediction, never dropped
        rec = {"idx": sample["idx"], "pred": {}, "error": f"{type(e).__name__}: {e}"}
    return rec


def _lenient(v):
    """Ignore differences that do not change meaning for AP: spaces, case, ',' vs '.'."""
    if isinstance(v, dict):
        return {k: _lenient(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_lenient(x) for x in v]
    if isinstance(v, str):
        return v.replace(" ", "").replace(",", ".").casefold()
    return v


def lenient_f1(preds: list[dict], gts: list[dict]) -> float:
    """Secondary metric, NOT comparable with published CORD numbers."""
    return JSONParseEvaluator().cal_f1([_lenient(p) for p in preds], [_lenient(g) for g in gts])


def per_key_f1(preds: list[dict], gts: list[dict]) -> dict[str, dict[str, float]]:
    """Field-level F1 per flattened key, same matching rule as Donut's global F1."""
    ev = JSONParseEvaluator()
    tp, fp, fn = Counter(), Counter(), Counter()
    for p, g in zip(preds, gts, strict=True):
        pf, gf = ev.flatten(ev.normalize_dict(p)), ev.flatten(ev.normalize_dict(g))
        for item in pf:
            if item in gf:
                tp[item[0]] += 1
                gf.remove(item)
            else:
                fp[item[0]] += 1
        for item in gf:
            fn[item[0]] += 1
    keys = sorted(set(tp) | set(fp) | set(fn), key=lambda k: -(tp[k] + fn[k]))
    return {
        k: {
            "support": tp[k] + fn[k],
            "f1": tp[k] / (tp[k] + (fp[k] + fn[k]) / 2) if tp[k] + fp[k] + fn[k] else 0.0,
        }
        for k in keys
    }


def score(records: list[dict], samples: list[dict], model: str) -> dict:
    ev = JSONParseEvaluator()
    by_idx = {s["idx"]: s for s in samples}
    records = sorted(records, key=lambda r: r["idx"])
    preds = [r["pred"] for r in records]
    gts = [by_idx[r["idx"]]["gt"] for r in records]
    accs = [ev.cal_acc(p, g) for p, g in zip(preds, gts, strict=True)]
    ok = [r for r in records if "error" not in r]
    lat = [r["latency_s"] for r in ok]
    return {
        "model": model,
        "n": len(records),
        "errors": len(records) - len(ok),
        "ted_accuracy": statistics.fmean(accs),
        "f1": ev.cal_f1(preds, gts),
        "f1_lenient": lenient_f1(preds, gts),
        "per_key": per_key_f1(preds, gts),
        "latency_p50_s": statistics.median(lat) if lat else None,
        "tokens": {
            "input": sum(r["usage"]["input_tokens"] for r in ok),
            "output": sum(r["usage"]["output_tokens"] for r in ok),
        },
        "baseline": DONUT_BASELINE,
        "failed": {r["idx"]: r["error"] for r in records if "error" in r},
    }


def format_report(rep: dict) -> str:
    b = rep["baseline"]
    out = [
        f"# CORD v2 {rep.get('split', 'test')} split: {rep['model']} (zero-shot), "
        f"prompt {rep.get('prompt', 'p1')}",
        "",
        f"- receipts: {rep['n']} (failed calls: {rep['errors']}, scored as empty)",
        "",
        "| model | training on CORD | TED accuracy | field F1 |",
        "|---|---|---|---|",
        f"| {rep['model']} | none (zero-shot) | {100 * rep['ted_accuracy']:.1f} | "
        f"{100 * rep['f1']:.1f} |",
        f"| {b['model']} | 800 receipts | {100 * b['ted_accuracy']:.1f} | {100 * b['f1']:.1f} |",
        "",
        f"Secondary, not comparable with published numbers: field F1 ignoring spaces, case, "
        f"and ',' vs '.' = {100 * rep.get('f1_lenient', 0):.1f}",
        "",
        "Per-field F1 (support = fields in ground truth):",
        "",
        "| field | support | F1 |",
        "|---|---|---|",
    ]
    out += [f"| {k} | {v['support']} | {100 * v['f1']:.1f} |" for k, v in rep["per_key"].items()]
    out += [
        "",
        f"- latency p50: {rep['latency_p50_s']} s; tokens: {rep['tokens']}",
        "",
        "Caveats: Donut was fine-tuned on the CORD train split, the vision LLM was not; "
        "CORD has been public since 2019 and may be in the LLM's pre-training data. "
        "Scores use Donut's evaluator on `gt_parse`, exact string match per field.",
    ]
    if rep["failed"]:
        out += ["", "Failed calls:"] + [f"- {k}: {v}" for k, v in rep["failed"].items()]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faktura-bench-cord")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--split", choices=["test", "validation"], default="test")
    parser.add_argument("--prompt", choices=sorted(PROMPTS), default="p1")
    parser.add_argument("--detail", choices=["low", "high", "auto"], default=None)
    args = parser.parse_args(argv)

    load_dotenv()
    client = AzureResponsesClient.from_env()
    samples = load_ground_truth(args.split)[: args.limit]
    raw_dir = args.run / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    prompt = PROMPTS[args.prompt]
    records: list[dict] = []
    # Submit one small batch at a time: at most `workers` images are held in memory.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = []
        for idx, image in iter_images(args.split, batch_size=args.workers):
            if idx >= len(samples):
                break
            pending.append(
                pool.submit(
                    run_one, client, {"idx": idx, "image": image}, raw_dir, prompt, args.detail
                )
            )
            del image
            if len(pending) >= args.workers:
                records += [f.result() for f in as_completed(pending)]
                pending = []
        records += [f.result() for f in as_completed(pending)]

    rep = score(records, samples, client.model)
    rep.update({"split": args.split, "prompt": args.prompt, "detail": args.detail})
    (args.run / "report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    report = format_report(rep)
    (args.run / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
