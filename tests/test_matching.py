import re
from pathlib import Path

import pytest

from faktura.evals.matching_eval import EXPECTED, evaluate, perfect_reading
from faktura.extract.schema import FieldName
from faktura.master import MasterData
from faktura.matching import ISSUE_POLICY, Ledger, Status, decide
from faktura.synth.cli import write_dataset
from faktura.synth.config import SynthConfig
from faktura.synth.generate import generate

VAT = 1100


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    cfg = SynthConfig(seed=31, n_purchase_orders=300, anomaly_rate=0.5)
    out = tmp_path_factory.mktemp("m")
    write_dataset(generate(cfg), cfg, out, render=False)
    return out


def test_every_validator_code_has_a_policy():
    src = (Path(__file__).parent.parent / "src" / "faktura" / "validate.py").read_text()
    codes = set(re.findall(r'code="(\w+)"', src))
    assert codes <= set(ISSUE_POLICY), codes - set(ISSUE_POLICY)


def test_oracle_reading_routes_every_invoice_as_expected(data_dir):
    rep = evaluate(data_dir, None)
    wrong = {k: v["decisions"] for k, v in rep["per_kind"].items() if v["routed_as_expected"] != 1}
    assert wrong == {}
    assert set(rep["per_kind"]) == {"clean"} | {a.value for a in EXPECTED if a}
    assert rep["money"].get("missed", 0) == 0
    assert rep["money"]["caught"] == rep["money"]["at_risk"] > 0
    assert rep["wrong_payments"] == []


def _first_clean(data_dir):
    from faktura.evals.extraction import load_labels

    return next(lb for lb in load_labels(data_dir) if not lb.anomalies)


def test_release_gate_flags_a_paid_anomaly():
    from faktura.evals.matching_eval import check

    def rep(held=1.0, routed=1.0, wrong=()):
        return {
            "per_kind": {
                "clean": {"not_auto_approved": 0.0, "routed_as_expected": 1.0},
                "price_mismatch": {"not_auto_approved": held, "routed_as_expected": routed},
            },
            "money": {"at_risk": 10, "caught": 10 if held == 1.0 else 5},
            "wrong_payments": list(wrong),
        }

    assert check(rep(), exact_routing=True) == []
    assert len(check(rep(held=0.5), exact_routing=False)) == 2  # paid, and money missed
    assert check(rep(routed=0.9), exact_routing=False) == []  # model readings may misroute
    assert check(rep(routed=0.9), exact_routing=True) != []
    assert check(rep(wrong=[{"invoice_id": "x"}]), exact_routing=False) != []


def test_misread_bank_account_is_held_not_paid(data_dir):
    md = MasterData.load(data_dir)
    lb = _first_clean(data_dir)
    ext = perfect_reading(lb.invoice).model_copy(update={"bank_account": "9" * 10})
    d = decide(lb.invoice_id, ext, md, Ledger(), VAT)
    assert d.status is Status.HOLD
    assert any(r.code == "bank_master" for r in d.reasons)


def test_vendor_without_trusted_bank_account_is_never_paid(data_dir):
    # The Odoo source leaves the account empty when no account is trusted (docs/erp.md).
    md = MasterData.load(data_dir)
    lb = _first_clean(data_dir)
    ext = perfect_reading(lb.invoice)
    assert decide(lb.invoice_id, ext, md, Ledger(), VAT).status is Status.APPROVE
    vid = next(v.vendor_id for v in md.vendors.values() if v.npwp == lb.invoice.vendor_npwp)
    md.vendors[vid] = md.vendors[vid].model_copy(update={"bank_account": "", "bank_name": ""})
    d = decide(lb.invoice_id, ext, md, Ledger(), VAT)
    assert d.status is Status.HOLD
    assert any(r.code == "bank_master" for r in d.reasons)


def test_resubmission_with_misread_number_is_still_a_duplicate(data_dir):
    md = MasterData.load(data_dir)
    lb = _first_clean(data_dir)
    ledger = Ledger()
    first = perfect_reading(lb.invoice)
    assert decide(lb.invoice_id, first, md, ledger, VAT).status is Status.APPROVE
    again = first.model_copy(
        update={"invoice_number": first.invoice_number + "X", "tax_invoice_number": "1" * 17}
    )
    d = decide("again", again, md, ledger, VAT)
    assert d.status is Status.HOLD
    assert any(r.code.startswith("duplicate") for r in d.reasons)


def test_duplicate_caught_when_the_original_was_misread(data_dir):
    # Regression: the original (a phone photo) had invoice, tax, and PO numbers misread;
    # its correctly read resubmission matched nothing and was approved.
    md = MasterData.load(data_dir)
    lb = _first_clean(data_dir)
    ledger = Ledger()
    truth = perfect_reading(lb.invoice)
    misread_original = truth.model_copy(
        update={
            "invoice_number": "INV/000/2025/0015",
            "tax_invoice_number": "2" * 18,
            "po_number": "PO/2025/99999",
        }
    )
    decide("orig", misread_original, md, ledger, VAT)
    d = decide("resubmitted", truth, md, ledger, VAT)
    assert d.status is Status.HOLD
    assert any(r.code == "duplicate_amounts" for r in d.reasons)


def test_different_amounts_are_not_duplicates(data_dir):
    md = MasterData.load(data_dir)
    lb = _first_clean(data_dir)
    ledger = Ledger()
    first = perfect_reading(lb.invoice)
    decide("a", first, md, ledger, VAT)
    other = first.model_copy(
        update={
            "invoice_number": first.invoice_number + "9",
            "tax_invoice_number": "3" * 17,
            "po_number": "PO/2025/99999",
            "total": first.total + 1000,
        }
    )
    d = decide("b", other, md, ledger, VAT)
    assert not any(r.code.startswith("duplicate") for r in d.reasons)


def test_model_uncertainty_sends_to_review(data_dir):
    md = MasterData.load(data_dir)
    lb = _first_clean(data_dir)
    ext = perfect_reading(lb.invoice).model_copy(update={"uncertain_fields": [FieldName.TOTAL]})
    assert decide(lb.invoice_id, ext, md, Ledger(), VAT).status is Status.REVIEW
    assert (
        decide(lb.invoice_id, ext, md, Ledger(), VAT, trust_model_flags=False).status
        is Status.APPROVE
    )


def test_most_severe_reason_wins(data_dir):
    md = MasterData.load(data_dir)
    lb = _first_clean(data_dir)
    ext = perfect_reading(lb.invoice).model_copy(
        update={"bank_account": "9" * 10, "total": lb.invoice.total + 1}  # hold + review
    )
    d = decide(lb.invoice_id, ext, md, Ledger(), VAT)
    assert {r.status for r in d.reasons} >= {Status.HOLD, Status.REVIEW}
    assert d.status is Status.HOLD
