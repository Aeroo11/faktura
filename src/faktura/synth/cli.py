"""`faktura-synth`: write a synthetic AP dataset to disk.

Output layout:
    <out>/vendors.json, purchase_orders.json, goods_receipts.json
    <out>/labels.jsonl          one InvoiceLabel per line (ground truth)
    <out>/invoices/<id>.pdf
    <out>/config.yaml           exact config used, for reproducibility
"""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel

from faktura.synth.config import SynthConfig, load_config
from faktura.synth.generate import Dataset, generate
from faktura.synth.render import render_invoice


def _dump_list(path: Path, items: list[BaseModel]) -> None:
    path.write_text(
        json.dumps([i.model_dump(mode="json") for i in items], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def write_dataset(ds: Dataset, cfg: SynthConfig, out: Path, render: bool = True) -> Dataset:
    out.mkdir(parents=True, exist_ok=True)
    # Separate stream so toggling rendering never changes the generated data.
    render_rng = random.Random(cfg.seed + 1)
    labels = []
    for lb in ds.labels:
        meta: dict[str, str | int | float | bool] = {}
        if render:
            meta = render_invoice(
                lb.invoice, out / "invoices" / f"{lb.invoice_id}.pdf", render_rng, cfg
            )
        labels.append(lb.model_copy(update={"render": meta}))

    _dump_list(out / "vendors.json", ds.vendors)
    _dump_list(out / "purchase_orders.json", ds.purchase_orders)
    _dump_list(out / "goods_receipts.json", ds.goods_receipts)
    with (out / "labels.jsonl").open("w", encoding="utf-8") as f:
        for lb in labels:
            f.write(lb.model_dump_json() + "\n")
    (out / "config.yaml").write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    return Dataset(ds.vendors, ds.purchase_orders, ds.goods_receipts, labels)


def summarize(ds: Dataset) -> str:
    counts = Counter(a.value for lb in ds.labels for a in lb.anomalies)
    clean = sum(1 for lb in ds.labels if not lb.anomalies)
    lines = [
        f"vendors={len(ds.vendors)} pos={len(ds.purchase_orders)} invoices={len(ds.labels)}",
        f"  clean: {clean}",
    ]
    lines += [f"  {k}: {v}" for k, v in sorted(counts.items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faktura-synth")
    parser.add_argument("--config", type=Path, default=Path("configs/synth.yaml"))
    parser.add_argument("--out", type=Path, default=Path("data/synth/v1"))
    parser.add_argument("--no-render", action="store_true", help="skip PDF rendering")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    ds = write_dataset(generate(cfg), cfg, args.out, render=not args.no_render)
    print(summarize(ds))
    print(f"written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
