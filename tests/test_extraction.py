from pathlib import Path

import pytest

from faktura.evals.extraction import evaluate, load_labels
from faktura.evals.extraction_scoring import score_fields
from faktura.extract.client import ExtractionResult, FakeClient, Usage
from faktura.extract.pdf import render_first_page_png
from faktura.extract.schema import ExtractedInvoice, ExtractedLine, FieldName
from faktura.schemas import Invoice
from faktura.synth.cli import main as synth_main


def perfect(inv: Invoice) -> ExtractedInvoice:
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


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("synth")
    cfg = root / "cfg.yaml"
    cfg.write_text("seed: 11\nn_purchase_orders: 8\nscan_effect_rate: 0.5\n", encoding="utf-8")
    out = root / "ds"
    synth_main(["--config", str(cfg), "--out", str(out)])
    return out


def test_perfect_extraction_scores_all_fields(dataset):
    inv = load_labels(dataset)[0].invoice
    assert all(score_fields(perfect(inv), inv).values())


def test_formatting_differences_are_normalized(dataset):
    inv = load_labels(dataset)[0].invoice
    spaced = " ".join(inv.vendor_npwp[i : i + 4] for i in range(0, 16, 4))
    ext = perfect(inv).model_copy(
        update={"vendor_npwp": spaced, "vendor_name": f"  {inv.vendor_name.upper()} "}
    )
    scores = score_fields(ext, inv)
    assert scores[FieldName.VENDOR_NPWP]
    assert scores[FieldName.VENDOR_NAME]


def test_single_wrong_digit_fails_only_that_field(dataset):
    inv = load_labels(dataset)[0].invoice
    ext = perfect(inv).model_copy(update={"total": inv.total + 1})
    scores = score_fields(ext, inv)
    assert not scores[FieldName.TOTAL]
    assert sum(not v for v in scores.values()) == 1


def test_missing_line_fails_lines(dataset):
    inv = load_labels(dataset)[0].invoice
    ext = perfect(inv)
    ext = ext.model_copy(update={"lines": ext.lines[:-1]})
    assert not score_fields(ext, inv)[FieldName.LINES]


def test_render_produces_png(dataset):
    lb = load_labels(dataset)[0]
    png = render_first_page_png(dataset / "invoices" / f"{lb.invoice_id}.pdf")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_evaluate_end_to_end_with_fake_client(dataset, tmp_path):
    labels = load_labels(dataset)
    client = FakeClient([perfect(lb.invoice) for lb in labels])
    rep = evaluate(client, dataset, tmp_path / "run", workers=1)
    assert rep["n"] == len(labels)
    assert rep["errors"] == 0
    assert rep["invoice_exact"] == 1.0
    assert (tmp_path / "run" / "report.md").exists()


def test_failures_are_scored_wrong_and_cache_resumes(dataset, tmp_path):
    labels = load_labels(dataset)

    class Flaky:
        model = "fake"

        def __init__(self):
            self.calls = 0

        def extract(self, image_png):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("boom")
            return ExtractionResult(perfect(labels[0].invoice), Usage(), 0.0, "fake")

    run = tmp_path / "run"
    flaky = Flaky()
    rep = evaluate(flaky, dataset, run, limit=1, workers=1)
    assert rep["errors"] == 1
    assert rep["per_field"]["total"] == 0.0

    rep = evaluate(flaky, dataset, run, limit=1, workers=1)  # failure is retried
    assert rep["errors"] == 0
    calls = flaky.calls
    evaluate(flaky, dataset, run, limit=1, workers=1)  # success is cached
    assert flaky.calls == calls
