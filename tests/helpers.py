"""Shared helpers for the API tests (fixtures are in conftest.py)."""

from faktura.app import service
from faktura.app.api import app
from faktura.evals.matching_eval import perfect_reading

STAFF = {"X-User": "budi", "X-Role": "ap_staff"}
STAFF2 = {"X-User": "dewi", "X-Role": "ap_staff"}
APPROVER = {"X-User": "sari", "X-Role": "approver"}
APPROVER2 = {"X-User": "rina", "X-Role": "approver"}
BUDI_AS_APPROVER = {"X-User": "budi", "X-Role": "approver"}


def ingest(env, lb, invoice_id=None, **overrides):
    """Ingest a label's invoice with a perfect reading, except for `overrides`."""
    _, _, data_dir = env
    reading = perfect_reading(lb.invoice).model_copy(update=overrides).model_dump(mode="json")
    with app.state.sessions() as s:
        row = service.ingest(
            s,
            app.state.ctx,
            invoice_id or lb.invoice_id,
            str(data_dir / "invoices" / f"{lb.invoice_id}.pdf"),
            reading,
        )
        s.commit()
        return row.id


def clean(labels):
    return [lb for lb in labels if not lb.anomalies]
