"""Against a real Postgres: migrations match the models, the audit table is append-only,
and the workflow (row locks, bank changes) runs end to end.

Skipped unless FAKTURA_TEST_DATABASE_URL points at an empty, disposable database (CI
starts one; locally e.g. a `faktura_test` database in the compose Postgres). The schema
is dropped again afterwards.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from faktura.app.api import app, load_context
from faktura.app.auth import DevHeaderAuth
from faktura.app.db import Base, make_sessionmaker
from faktura.evals.extraction import load_labels
from tests.helpers import APPROVER, STAFF, STAFF2, clean, ingest

URL = os.environ.get("FAKTURA_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not URL, reason="FAKTURA_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def pg(monkeypatch_module):
    monkeypatch_module.setenv("DATABASE_URL", URL)  # read by migrations/env.py
    cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_engine(URL)
    yield engine
    engine.dispose()
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def test_migrations_match_the_models(pg):
    with pg.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_audit_rows_cannot_be_updated_or_deleted(pg):
    with pg.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_events (seq, invoice_id, at, actor, action, payload, "
                "prev_hash, hash) VALUES (999999, NULL, 't', 'x', 'probe', '{}', 'p', 'probe-h')"
            )
        )
    for stmt in (
        "UPDATE audit_events SET actor = 'mallory' WHERE seq = 999999",
        "DELETE FROM audit_events WHERE seq = 999999",
    ):
        with pytest.raises(DBAPIError, match="append-only"), pg.begin() as conn:
            conn.execute(text(stmt))


def test_bank_change_workflow_on_postgres(pg, app_data_dir):
    with pg.begin() as conn:  # the probe row above is append-only; start from a clean chain
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete"))
        conn.execute(text("DELETE FROM audit_events"))
        conn.execute(text("DELETE FROM audit_head"))
        conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete"))
    app.state.sessions = make_sessionmaker(pg)
    app.state.ctx = load_context(app_data_dir)
    app.state.auth = DevHeaderAuth()
    labels = load_labels(app_data_dir)
    with TestClient(app, headers=STAFF) as client:
        env = (client, labels, app_data_dir)
        inv = ingest(env, clean(labels)[0], bank_account="7712345678")
        client.post(f"/invoices/{inv}/confirmations", json={"field": "bank_account"})
        r = client.post("/bank-changes", json={"source_invoice_id": inv, "channel": "on invoice"})
        cid = r.json()["id"]
        r = client.post(
            f"/bank-changes/{cid}/callback",
            headers=STAFF2,
            json={"spoke_with": "finance", "outcome": "confirmed"},
        )
        assert r.json()["status"] == "awaiting_approval"
        r = client.post(f"/bank-changes/{cid}/approve", headers=APPROVER, json={})
        assert r.json()["status"] == "approved"
        assert client.get(f"/invoices/{inv}").json()["state"] == "awaiting_approval"
        assert client.get("/audit/verify").json()["ok"]
