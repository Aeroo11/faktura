import pytest

from faktura.evals.validation import ANOMALY_FIELDS, history_excluding
from faktura.extract.schema import ExtractedLine, FieldName
from faktura.master import MasterData
from faktura.synth.cli import write_dataset
from faktura.synth.config import SynthConfig
from faktura.synth.generate import generate
from faktura.validate import (
    IssueKind,
    check_arithmetic,
    check_format,
    number_shape,
    suspect_fields,
    validate,
)
from tests.test_extraction import perfect

VAT_BPS = 1100


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    cfg = SynthConfig(seed=21, n_purchase_orders=250, anomaly_rate=0.5)
    out = tmp_path_factory.mktemp("w")
    ds = write_dataset(generate(cfg), cfg, out, render=False)
    md = MasterData.load(out)
    return ds.labels, md, history_excluding(ds.labels, md)


def _validate(lb, ext, md, history):
    md.approved_invoice_numbers = history[lb.invoice_id]
    return validate(ext, md, VAT_BPS)


def test_perfect_reading_of_clean_invoices_raises_nothing(world):
    labels, md, history = world
    noisy = {
        lb.invoice_id: [i.code for i in _validate(lb, perfect(lb.invoice), md, history)]
        for lb in labels
        if not lb.anomalies
    }
    assert {k: v for k, v in noisy.items() if v} == {}


def test_perfect_reading_of_anomalies_flags_only_the_expected_fields(world):
    labels, md, history = world
    checked = 0
    for lb in labels:
        if not lb.anomalies or lb.duplicate_of:
            continue
        expected = set().union(*(ANOMALY_FIELDS[a] for a in lb.anomalies))
        got = suspect_fields(_validate(lb, perfect(lb.invoice), md, history))
        assert got == expected, (lb.invoice_id, lb.anomalies, got)
        checked += 1
    assert checked > 20


def test_typical_misreads_are_caught(world):
    labels, md, history = world
    lb = next(lb for lb in labels if not lb.anomalies)
    base = perfect(lb.invoice)
    cases = {
        # doubled digit -> wrong length
        FieldName.VENDOR_NPWP: base.model_copy(update={"vendor_npwp": base.vendor_npwp + "1"}),
        # zero read as letter O -> shape no longer matches the vendor's numbering
        FieldName.INVOICE_NUMBER: base.model_copy(
            update={"invoice_number": base.invoice_number.replace("V0", "VO", 1)}
        ),
        # total misread -> arithmetic breaks
        FieldName.TOTAL: base.model_copy(update={"total": base.total + 100_000}),
    }
    for field, ext in cases.items():
        assert field in suspect_fields(_validate(lb, ext, md, history)), field


@pytest.mark.parametrize(
    "field",
    [
        FieldName.VAT,
        FieldName.DPP,
        FieldName.TOTAL,
        FieldName.VENDOR_NPWP,
        FieldName.BANK_ACCOUNT,
        FieldName.INVOICE_NUMBER,
        FieldName.TAX_INVOICE_NUMBER,
        FieldName.INVOICE_DATE,
        FieldName.PO_NUMBER,
    ],
)
def test_unreadable_money_and_identifier_fields_are_flagged(world, field):
    # Regression: a VAT hidden under a stamp came back null and was never flagged.
    labels, _, _ = world
    ext = perfect(labels[0].invoice).model_copy(update={field.value: None})
    assert field in suspect_fields(check_format(ext))


def test_number_shape():
    assert number_shape("INV/V007/2025/0004") == "AAA/A999/9999/9999"
    assert number_shape("INV/VO07/2025/0004") == "AAA/AA99/9999/9999"


def test_format_and_arithmetic_kinds(world):
    labels, _, _ = world
    ext = perfect(labels[0].invoice).model_copy(
        update={
            "vendor_npwp": "123",
            "invoice_date": "18-11-2025",
            "lines": [ExtractedLine(description="x", qty=2, unit_price=10, amount=21)],
        }
    )
    kinds = {i.code: i.kind for i in check_format(ext) + check_arithmetic(ext)}
    assert kinds["npwp_length"] is IssueKind.FORMAT
    assert kinds["date_unparseable"] is IssueKind.FORMAT
    assert kinds["line_amount"] is IssueKind.INCONSISTENT
