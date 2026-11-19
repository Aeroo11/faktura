"""`faktura-eval-learn`: does learning from corrections cut false fraud alarms, safely?

Replays the 340 invoices in arrival order with the real model readings. Whenever an
invoice goes to a person, the simulated AP clerk checks the flagged fields against the
page: a misread identifier is corrected to the printed value (a correction the misread
model can learn from), a correct one is confirmed. The misread model used for invoice i
is fitted only on corrections made to invoices before i.

Reported, baseline (no learning) vs. learned:
- fraud_hold queue: how many invoices, how many of them clean (false alarms)
- real NPWP / bank-account anomalies triaged as probable misreads, and where each ends up
  after the page check (it must be held: the page shows the changed value)
- learning curve: false alarms in the second half of the stream vs. the number of
  corrections from the first half the model was fitted on
- adversarial: an attacker who knows the reader's most common misread changes one digit
  of a vendor's bank account or NPWP to exactly that; how far does the invoice get?
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from faktura.evals.extraction import load_labels
from faktura.evals.matching_eval import load_readings, perfect_reading
from faktura.evals.validation import history_excluding
from faktura.extract.schema import ExtractedInvoice
from faktura.learn.misread import DIGIT_FIELDS, Correction, MisreadModel, digits
from faktura.master import MasterData
from faktura.matching import Decision, Ledger, Status, decide
from faktura.schemas import InvoiceLabel
from faktura.synth.config import load_config
from faktura.validate import validate

MASTER_CODES = {"npwp_master", "bank_master", "bank_name_master"}
PAYMENT_DETAIL = {"npwp_mismatch", "bank_account_changed"}  # the anomalies fraud_hold is for
CURVE = (0, 10, 25, 50, 100)


@dataclass
class Step:
    invoice_id: str
    kind: str
    base: Decision
    learned: Decision
    corrections: list[Correction]
    held_after_check: bool  # learned path: on fraud_hold once a person checked the page


def _kind(lb: InvoiceLabel) -> str:
    return lb.anomalies[0].value if lb.anomalies else "clean"


def page_check(
    ext: ExtractedInvoice, lb: InvoiceLabel, d: Decision
) -> tuple[ExtractedInvoice, list[Correction], set[str]]:
    """The clerk compares every flagged identifier with the page and fixes misreads."""
    flagged = {f for r in d.reasons for f in r.fields}
    update, corrections = {}, []
    for f in flagged & set(DIGIT_FIELDS):
        printed = getattr(lb.invoice, f)
        read = getattr(ext, f)
        if digits(read) != digits(printed):
            corrections.append(Correction(f, read, printed))
            update[f] = printed
    return ext.model_copy(update=update), corrections, flagged


def held_after_check(d: Decision, checked: ExtractedInvoice, md: MasterData, vat_bps: int) -> bool:
    """Still on fraud_hold once the flagged fields match the page."""
    if any(r.status is Status.HOLD and r.code not in MASTER_CODES for r in d.reasons):
        return True  # duplicates and other holds are not about reading
    if d.status is Status.APPROVE:
        return False
    return any(i.code in MASTER_CODES for i in validate(checked, md, vat_bps))


def replay(
    labels: list[InvoiceLabel],
    readings: dict[str, ExtractedInvoice],
    md: MasterData,
    history: dict,
    vat_bps: int,
    fixed_model: MisreadModel | None = None,
    learn: bool = True,
) -> tuple[list[Step], list[Correction]]:
    """Run the stream. With `fixed_model` the model does not change while replaying."""
    model = fixed_model or MisreadModel()
    ledgers = (Ledger(), Ledger())
    steps, all_corrections = [], []
    for lb in labels:
        ext = readings.get(lb.invoice_id)
        if ext is None:
            continue
        md.approved_invoice_numbers = history[lb.invoice_id]
        base = decide(lb.invoice_id, ext, md, ledgers[0], vat_bps)
        learned = decide(
            lb.invoice_id, ext, md, ledgers[1], vat_bps, misread=model if learn else None
        )
        corrections: list[Correction] = []
        held = False
        if learned.status is not Status.APPROVE:
            checked, corrections, _ = page_check(ext, lb, learned)
            held = held_after_check(learned, checked, md, vat_bps)
        steps.append(Step(lb.invoice_id, _kind(lb), base, learned, corrections, held))
        all_corrections += corrections
        if fixed_model is None and learn:
            for c in corrections:  # available from the next invoice on
                model.add(c)
    return steps, all_corrections


def queue_stats(steps: list[Step], which: str) -> dict:
    holds = [s for s in steps if getattr(s, which).status is Status.HOLD]
    return {
        "fraud_hold": len(holds),
        "clean_in_fraud_hold": sum(s.kind == "clean" for s in holds),
        "anomalies_in_fraud_hold": sum(s.kind != "clean" for s in holds),
        "payment_detail_anomalies": sum(s.kind in PAYMENT_DETAIL for s in steps),
        "payment_detail_in_fraud_hold": sum(s.kind in PAYMENT_DETAIL for s in holds),
        "precision": round(sum(s.kind != "clean" for s in holds) / len(holds), 3)
        if holds
        else None,
        "auto_approved": sum(getattr(s, which).status is Status.APPROVE for s in steps),
    }


def _triaged(s: Step) -> bool:
    return any(r.code == "probable_misread" for r in s.learned.reasons)


def adversarial(
    labels: list[InvoiceLabel], md: MasterData, vat_bps: int, model: MisreadModel
) -> dict:
    """Per clean invoice: change one identifier digit using the most-seen known substitution."""
    subs = [
        (e.split(">")[0], e.split(">")[1])
        for e, n in model.edit_counts.most_common()
        if ">" in e and model.known(e)
    ]
    out = Counter()
    for lb in labels:
        if lb.anomalies:
            continue
        for field_name in ("bank_account", "vendor_npwp"):
            value = getattr(lb.invoice, field_name)
            attack = next((value.replace(a, b, 1) for a, b in subs if a in value), None)
            if attack is None:
                continue
            forged = perfect_reading(lb.invoice).model_copy(update={field_name: attack})
            md.approved_invoice_numbers = {}
            d = decide(lb.invoice_id, forged, md, Ledger(), vat_bps, misread=model)
            out["attempts"] += 1
            out[f"first_decision_{d.status.value}"] += 1
            if d.status is Status.APPROVE:
                out["paid_automatically"] += 1
            # The clerk checks the page, which shows the forged digits: confirm as read.
            again = decide(
                lb.invoice_id, forged, md, Ledger(), vat_bps, misread=model, verified={field_name}
            )
            out["held_after_page_check"] += again.status is Status.HOLD
    return dict(out)


def evaluate(data_dir: Path, run_dirs: list[Path]) -> dict:
    vat_bps = load_config(data_dir / "config.yaml").vat_bps
    labels = load_labels(data_dir)
    md = MasterData.load(data_dir)
    history = history_excluding(labels, md)
    readings = load_readings(run_dirs)

    steps, corrections = replay(labels, readings, md, history, vat_bps)
    final_model = MisreadModel.fit(corrections)
    final_model.trained_on = {
        "source": "simulated clerk corrections (faktura-eval-learn)",
        "data": str(data_dir),
        "runs": [str(r) for r in run_dirs],
    }

    triaged = [s for s in steps if _triaged(s)]
    triaged_anomalies = [s for s in triaged if s.kind != "clean"]
    out = {
        "invoices": len(steps),
        "corrections": {"identifier": len(corrections), "used": final_model.n_corrections},
        "baseline": queue_stats(steps, "base"),
        "learned": queue_stats(steps, "learned"),
        "triaged_as_misread": {
            "clean": sum(s.kind == "clean" for s in triaged),
            "anomalies": Counter(s.kind for s in triaged_anomalies),
            "anomalies_held_after_page_check": sum(s.held_after_check for s in triaged_anomalies),
            "clean_cleared_by_page_check": sum(
                s.kind == "clean" and not s.held_after_check for s in triaged
            ),
        },
        "learned_edits": dict(final_model.edit_counts.most_common(12)),
    }

    # Learning curve on a fixed split: fit on the first N corrections from the first half,
    # measure on the second half only.
    half = len(labels) // 2
    first, second = labels[:half], labels[half:]
    _, train = replay(first, readings, md, history, vat_bps, learn=False)
    curve = []
    for n in [*CURVE, len(train)]:
        if n > len(train):
            continue
        model = MisreadModel.fit(train[:n])
        st, _ = replay(labels, readings, md, history, vat_bps, fixed_model=model)
        test = [s for s in st if s.invoice_id in {lb.invoice_id for lb in second}]
        tri = [s for s in test if _triaged(s)]
        curve.append(
            {
                "corrections": n,
                "known_edits": sum(model.known(e) for e in model.edit_counts),
                "clean_in_fraud_hold": queue_stats(test, "learned")["clean_in_fraud_hold"],
                "fraud_hold_precision": queue_stats(test, "learned")["precision"],
                "payment_detail_in_fraud_hold": queue_stats(test, "learned")[
                    "payment_detail_in_fraud_hold"
                ],
                "payment_detail_anomalies": queue_stats(test, "learned")[
                    "payment_detail_anomalies"
                ],
                "clean_triaged": sum(s.kind == "clean" for s in tri),
            }
        )
    out["curve"] = curve
    out["curve_baseline_clean_in_fraud_hold"] = queue_stats(
        [s for s in steps if s.invoice_id in {lb.invoice_id for lb in second}], "base"
    )["clean_in_fraud_hold"]
    out["adversarial"] = adversarial(second, md, vat_bps, MisreadModel.fit(train))
    out["model_version"] = final_model.version
    out["_model"] = final_model
    return out


def format_report(rep: dict) -> str:
    b, l_ = rep["baseline"], rep["learned"]
    t = rep["triaged_as_misread"]
    lines = [
        "# Learning from corrections",
        "",
        f"{rep['invoices']} invoices in arrival order; {rep['corrections']['identifier']} "
        f"identifier corrections made along the way ({rep['corrections']['used']} usable).",
        "",
        "| | Baseline | Learned |",
        "|---|---|---|",
        f"| fraud_hold | {b['fraud_hold']} | {l_['fraud_hold']} |",
        f"| clean invoices in fraud_hold (false alarms) | {b['clean_in_fraud_hold']} "
        f"| {l_['clean_in_fraud_hold']} |",
        f"| real anomalies in fraud_hold | {b['anomalies_in_fraud_hold']} "
        f"| {l_['anomalies_in_fraud_hold']} |",
        f"| fraud_hold precision | {b['precision']} | {l_['precision']} |",
        f"| changed NPWP / bank account (of {b['payment_detail_anomalies']}) still in fraud_hold "
        f"| {b['payment_detail_in_fraud_hold']} | {l_['payment_detail_in_fraud_hold']} |",
        f"| approved automatically | {b['auto_approved']} | {l_['auto_approved']} |",
        "",
        f"Triaged as probable misread: {t['clean']} clean, "
        f"{sum(t['anomalies'].values())} real anomalies {dict(t['anomalies'])}. "
        f"After the page check: {t['anomalies_held_after_page_check']} of the anomalies held, "
        f"{t['clean_cleared_by_page_check']} clean invoices cleared.",
        "",
        "## Learning curve (second half of the stream)",
        "",
        f"Baseline false alarms in the second half: {rep['curve_baseline_clean_in_fraud_hold']}.",
        "",
        "| Corrections | Known edits | False alarms | Precision | Changed NPWP/bank held |",
        "|---|---|---|---|---|",
    ]
    for c in rep["curve"]:
        lines.append(
            f"| {c['corrections']} | {c['known_edits']} | {c['clean_in_fraud_hold']} | "
            f"{c['fraud_hold_precision']} | "
            f"{c['payment_detail_in_fraud_hold']}/{c['payment_detail_anomalies']} |"
        )
    lines += [
        "",
        "## Adversarial: one digit changed to the most common misread",
        "",
        "```",
        json.dumps(rep["adversarial"], indent=2),
        "```",
        "",
        "## Learned edits (most common)",
        "",
        "```",
        json.dumps(rep["learned_edits"], indent=2),
        "```",
    ]
    return "\n".join(lines) + "\n"


def fit_on_run(data_dir: Path, run_dirs: list[Path], subset: Path, out: Path) -> int:
    vat_bps = load_config(data_dir / "config.yaml").vat_bps
    labels = load_labels(data_dir)
    md = MasterData.load(data_dir)
    ids = {p.stem for p in (subset / "raw").glob("*.json")}
    steps, _ = replay(
        labels, load_readings(run_dirs), md, history_excluding(labels, md), vat_bps, learn=False
    )
    model = MisreadModel.fit(c for s in steps if s.invoice_id in ids for c in s.corrections)
    model.trained_on = {
        "source": "simulated clerk corrections (faktura-eval-learn --fit-on)",
        "invoices": sorted(ids),
        "runs": [str(r) for r in run_dirs],
    }
    out.mkdir(parents=True, exist_ok=True)
    model.save(out / "misread-model.json")
    print(model.version, dict(model.edit_counts.most_common()), "\nhint:", model.hint())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=Path("data/synth/v2"))
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, default=Path("runs/learn-eval"))
    parser.add_argument(
        "--fit-on",
        type=Path,
        help="only write a model fitted on the corrections to the invoices in this run dir "
        "(e.g. the re-read dev set), to give a held-out re-read eval hints that never saw it",
    )
    args = parser.parse_args(argv)
    if args.fit_on:
        return fit_on_run(args.data, args.runs, args.fit_on, args.out)
    rep = evaluate(args.data, args.runs)
    args.out.mkdir(parents=True, exist_ok=True)
    model = rep.pop("_model")
    model.save(args.out / "misread-model.json")
    (args.out / "report.json").write_text(json.dumps(rep, indent=2, default=dict), "utf-8")
    text = format_report(rep)
    (args.out / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
