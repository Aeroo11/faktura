"""`faktura-eval-match`: end-to-end decisions on the synthetic dataset.

Runs the three-way match over every invoice in arrival order, using either a real
extraction (merged from one or more run directories, first match wins) or a perfect
"oracle" reading of the ground truth. The oracle run isolates the matching logic; the
real run shows what reading errors cost.

Reported:
- per anomaly type: caught (not auto-approved) and routed to the expected decision
- clean invoices: share approved straight through
- money: rupiah at risk on anomalous invoices, caught vs missed
- wrong payments: approved invoices whose amount or bank account was misread
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from faktura.evals.extraction import load_labels
from faktura.evals.validation import history_excluding
from faktura.extract.schema import ExtractedInvoice, ExtractedLine
from faktura.master import MasterData
from faktura.matching import Ledger, Status, decide
from faktura.schemas import AnomalyType, Invoice, InvoiceLabel, compute_vat
from faktura.synth.config import load_config

EXPECTED: dict[AnomalyType | None, Status] = {
    None: Status.APPROVE,
    AnomalyType.PRICE_MISMATCH: Status.EXCEPTION,
    AnomalyType.QTY_OVER_RECEIVED: Status.EXCEPTION,
    AnomalyType.VAT_MISCALC: Status.EXCEPTION,
    AnomalyType.NPWP_MISMATCH: Status.HOLD,
    AnomalyType.BANK_ACCOUNT_CHANGED: Status.HOLD,
    AnomalyType.DUPLICATE_INVOICE: Status.HOLD,
}
WHOLE_AMOUNT_AT_RISK = {
    AnomalyType.NPWP_MISMATCH,
    AnomalyType.BANK_ACCOUNT_CHANGED,
    AnomalyType.DUPLICATE_INVOICE,
}


def perfect_reading(inv: Invoice) -> ExtractedInvoice:
    return ExtractedInvoice(
        invoice_number=inv.invoice_number,
        tax_invoice_number=inv.tax_invoice_number,
        invoice_date=inv.invoice_date.isoformat(),
        po_number=inv.po_number,
        vendor_name=inv.vendor_name,
        vendor_npwp=inv.vendor_npwp,
        bank_name=inv.bank_name,
        bank_account=inv.bank_account,
        lines=[
            ExtractedLine(
                description=ln.description, qty=ln.qty, unit_price=ln.unit_price, amount=ln.amount
            )
            for ln in inv.lines
        ],
        dpp=inv.dpp,
        vat=inv.vat,
        total=inv.total,
        uncertain_fields=[],
    )


def amount_at_risk(lb: InvoiceLabel, md: MasterData, vat_bps: int) -> int:
    """Rupiah lost if this invoice were paid as printed."""
    if not lb.anomalies:
        return 0
    kind = lb.anomalies[0]
    if kind in WHOLE_AMOUNT_AT_RISK:
        return lb.invoice.total
    po = md.purchase_orders[lb.invoice.po_number]
    price = {ln.sku: ln.unit_price for ln in po.lines}
    received = {g.sku: g.qty_received for g in md.goods_receipts[po.po_number].lines}
    dpp = sum(min(ln.qty, received[ln.sku]) * price[ln.sku] for ln in lb.invoice.lines)
    return max(0, lb.invoice.total - (dpp + compute_vat(dpp, vat_bps)))


def load_readings(run_dirs: list[Path]) -> dict[str, ExtractedInvoice]:
    out: dict[str, ExtractedInvoice] = {}
    for run in run_dirs:
        for p in sorted((run / "raw").glob("*.json")):
            if p.stem in out:
                continue
            rec = json.loads(p.read_text(encoding="utf-8"))
            if "extracted" in rec:
                out[p.stem] = ExtractedInvoice.model_validate(rec["extracted"])
    return out


def evaluate(data_dir: Path, readings: dict[str, ExtractedInvoice] | None) -> dict:
    vat_bps = load_config(data_dir / "config.yaml").vat_bps
    labels = load_labels(data_dir)  # file order = arrival order
    md = MasterData.load(data_dir)
    history = history_excluding(labels, md)
    ledger = Ledger()

    confusion: dict[str, Counter] = defaultdict(Counter)
    by_level: dict[str, Counter] = defaultdict(Counter)
    money = Counter()
    wrong_payments = []
    missing = 0
    for lb in labels:
        if readings is None:
            ext = perfect_reading(lb.invoice)
        elif lb.invoice_id in readings:
            ext = readings[lb.invoice_id]
        else:  # no reading at all: it must go to a human
            missing += 1
            confusion[_kind(lb)][Status.REVIEW.value] += 1
            continue
        md.approved_invoice_numbers = history[lb.invoice_id]
        d = decide(lb.invoice_id, ext, md, ledger, vat_bps, trust_model_flags=readings is not None)
        kind = _kind(lb)
        confusion[kind][d.status.value] += 1
        level = str(lb.render.get("level", "n/a"))
        by_level[level]["n"] += 1
        by_level[level][f"{'clean' if not lb.anomalies else 'anomaly'}_{d.status.value}"] += 1

        risk = amount_at_risk(lb, md, vat_bps)
        if risk:
            money["at_risk"] += risk
            money["caught" if d.status is not Status.APPROVE else "missed"] += risk
        if (
            d.status is Status.APPROVE
            and not lb.anomalies
            and (ext.total != lb.invoice.total or ext.bank_account != lb.invoice.bank_account)
        ):
            wrong_payments.append(
                {"invoice_id": lb.invoice_id, "paid": ext.total, "correct": lb.invoice.total}
            )

    per_kind = {}
    for kind, c in sorted(confusion.items()):
        n = sum(c.values())
        expected = EXPECTED[None if kind == "clean" else AnomalyType(kind)].value
        per_kind[kind] = {
            "n": n,
            "decisions": dict(c),
            "expected": expected,
            "routed_as_expected": c[expected] / n,
            "not_auto_approved": 1 - c[Status.APPROVE.value] / n,
        }
    return {
        "mode": "oracle" if readings is None else "model",
        "per_kind": per_kind,
        "money": dict(money),
        "wrong_payments": wrong_payments,
        "missing_readings": missing,
        "by_level": {k: dict(v) for k, v in sorted(by_level.items())},
    }


def _kind(lb: InvoiceLabel) -> str:
    return lb.anomalies[0].value if lb.anomalies else "clean"


def _rp(n: int) -> str:
    return "Rp " + f"{n:,}".replace(",", ".")


def format_report(rep: dict) -> str:
    out = [
        f"# Match eval ({rep['mode']} reading)",
        "",
        "| invoice kind | n | expected | routed as expected | not auto-approved | decisions |",
        "|---|---|---|---|---|---|",
    ]
    for kind, v in rep["per_kind"].items():
        out.append(
            f"| {kind} | {v['n']} | {v['expected']} | {100 * v['routed_as_expected']:.1f}% | "
            f"{100 * v['not_auto_approved']:.1f}% | {v['decisions']} |"
        )
    m = rep["money"]
    at_risk = m.get("at_risk", 0)
    out += [
        "",
        f"- money at risk on anomalous invoices: {_rp(at_risk)}; caught "
        f"{_rp(m.get('caught', 0))} ({100 * m.get('caught', 0) / at_risk:.1f}%), "
        f"missed {_rp(m.get('missed', 0))}"
        if at_risk
        else "- no money at risk",
        f"- clean invoices approved with a misread amount or bank account: "
        f"{len(rep['wrong_payments'])} {rep['wrong_payments'][:5]}",
        f"- invoices without any reading (sent to review): {rep['missing_readings']}",
    ]
    if rep["by_level"]:
        out += [
            "",
            "| level | n | clean approved | clean to human | anomalies caught |",
            "|---|---|---|---|---|",
        ]
        for lvl, c in rep["by_level"].items():
            clean = sum(v for k, v in c.items() if k.startswith("clean_"))
            anom = sum(v for k, v in c.items() if k.startswith("anomaly_"))
            out.append(
                f"| {lvl} | {c['n']} | {c.get('clean_approve', 0)}/{clean} | "
                f"{clean - c.get('clean_approve', 0)}/{clean} | "
                f"{anom - c.get('anomaly_approve', 0)}/{anom} |"
            )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faktura-eval-match")
    parser.add_argument("--data", type=Path, default=Path("data/synth/v2"))
    parser.add_argument(
        "--runs",
        type=Path,
        nargs="*",
        default=[],
        help="extraction run dirs, first match wins; omit for --oracle",
    )
    parser.add_argument("--oracle", action="store_true", help="use perfect readings")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if not args.oracle and not args.runs:
        parser.error("give --runs or --oracle")

    rep = evaluate(args.data, None if args.oracle else load_readings(args.runs))
    text = format_report(rep)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "match.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
        (args.out / "match.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
