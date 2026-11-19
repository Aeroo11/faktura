"""`faktura-eval-validate`: how many extraction errors do the validators catch?

Re-uses the cached model outputs of an extraction run, so it makes no API calls.
Compares three gates for "this field needs a human":
    self-flags   the model's own `uncertain_fields`
    validators   deterministic rules in faktura.validate
    union        either of the two
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from faktura.evals.extraction import load_labels
from faktura.evals.extraction_scoring import score_fields
from faktura.extract.schema import ExtractedInvoice, FieldName
from faktura.master import MasterData
from faktura.schemas import AnomalyType, InvoiceLabel
from faktura.synth.config import load_config
from faktura.validate import suspect_fields, validate

# Fields a genuine anomaly is expected to make suspicious. A flag on these fields of an
# anomalous invoice is a correct business flag, not a false alarm.
ANOMALY_FIELDS: dict[AnomalyType, set[FieldName]] = {
    AnomalyType.PRICE_MISMATCH: {FieldName.LINES},
    AnomalyType.QTY_OVER_RECEIVED: {FieldName.LINES},
    AnomalyType.NPWP_MISMATCH: {FieldName.VENDOR_NPWP},
    AnomalyType.VAT_MISCALC: {FieldName.VAT},
    AnomalyType.BANK_ACCOUNT_CHANGED: {FieldName.BANK_ACCOUNT},
    AnomalyType.DUPLICATE_INVOICE: set(),
}


def history_excluding(labels: list[InvoiceLabel], md: MasterData) -> dict[str, dict[str, list]]:
    """Per invoice: the vendor's other invoice numbers (leave-one-out 'approved history')."""
    by_vendor: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for lb in labels:
        vid = md.purchase_orders[lb.invoice.po_number].vendor_id
        by_vendor[vid].append((lb.invoice_id, lb.invoice.invoice_number))
    out = {}
    for lb in labels:
        vid = md.purchase_orders[lb.invoice.po_number].vendor_id
        own = lb.invoice.invoice_number
        out[lb.invoice_id] = {vid: [n for i, n in by_vendor[vid] if n != own]}
    return out


def evaluate(data_dir: Path, run_dir: Path, vat_bps: int) -> dict:
    labels = {lb.invoice_id: lb for lb in load_labels(data_dir)}
    md = MasterData.load(data_dir)
    history = history_excluding(list(labels.values()), md)

    totals = defaultdict(int)
    per_level = defaultdict(lambda: defaultdict(int))
    for path in sorted((run_dir / "raw").glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        lb = labels[rec["invoice_id"]]
        ext = ExtractedInvoice.model_validate(rec["extracted"])
        md.approved_invoice_numbers = history[lb.invoice_id]

        wrong = {f for f, ok in score_fields(ext, lb.invoice).items() if not ok}
        by_rules = suspect_fields(validate(ext, md, vat_bps))
        by_self = set(ext.uncertain_fields)
        expected = (
            set().union(*(ANOMALY_FIELDS[a] for a in lb.anomalies)) if lb.anomalies else set()
        )
        level = lb.render.get("level", "n/a")

        t = totals
        lv = per_level[level]
        for bucket in (t, lv):
            bucket["invoices"] += 1
            bucket["field_errors"] += len(wrong)
            bucket["caught_self"] += len(wrong & by_self)
            bucket["caught_rules"] += len(wrong & by_rules)
            bucket["caught_union"] += len(wrong & (by_rules | by_self))
            correct_flagged = (by_rules | by_self) - wrong
            bucket["flags_on_correct_explained"] += len(correct_flagged & expected)
            bucket["false_alarms"] += len(correct_flagged - expected)
            if wrong:
                bucket["misread_invoices"] += 1
                bucket["misread_slipped_rules"] += not by_rules
                bucket["misread_slipped_self"] += not by_self
                bucket["misread_slipped_union"] += not (by_rules | by_self)
            if not wrong and not lb.anomalies:
                bucket["clean_read_ok"] += 1
                bucket["clean_read_ok_passed"] += not (by_rules | by_self)

    def summarize(b: dict) -> dict:
        def r(n: str, d: str) -> float | None:
            return b[n] / b[d] if b[d] else None

        return {
            **dict(b),
            "error_recall_self": r("caught_self", "field_errors"),
            "error_recall_rules": r("caught_rules", "field_errors"),
            "error_recall_union": r("caught_union", "field_errors"),
            "misread_slip_rate_union": r("misread_slipped_union", "misread_invoices"),
            "straight_through_rate": r("clean_read_ok_passed", "clean_read_ok"),
        }

    return {
        "overall": summarize(totals),
        "by_level": {str(k): summarize(v) for k, v in sorted(per_level.items(), key=str)},
    }


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def format_report(rep: dict) -> str:
    o = rep["overall"]
    rows = [("all", o)] + [(f"level {k}", v) for k, v in rep["by_level"].items()]
    out = [
        "# Validator eval",
        "",
        "Share of field-level extraction errors sent to a human, by gate:",
        "",
        "| slice | field errors | self-flags | validators | union | misread invoices "
        "that slip through | clean invoices auto-passed |",
        "|---|---|---|---|---|---|---|",
    ]
    out += [
        f"| {name} | {b['field_errors']} | {_p(b['error_recall_self'])} | "
        f"{_p(b['error_recall_rules'])} | {_p(b['error_recall_union'])} | "
        f"{b['misread_slipped_union']}/{b['misread_invoices']} | "
        f"{_p(b['straight_through_rate'])} |"
        for name, b in rows
    ]
    out += [
        "",
        f"- flags on correctly read fields explained by a real anomaly: "
        f"{o['flags_on_correct_explained']}",
        f"- false alarms (flag on a correct field with no anomaly behind it): {o['false_alarms']}",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faktura-eval-validate")
    parser.add_argument("--data", type=Path, default=Path("data/synth/v2"))
    parser.add_argument("--run", type=Path, required=True, help="extraction run with raw/")
    args = parser.parse_args(argv)
    cfg = load_config(args.data / "config.yaml")  # the exact config the dataset was built with
    rep = evaluate(args.data, args.run, cfg.vat_bps)
    (args.run / "validation.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    report = format_report(rep)
    (args.run / "validation.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
