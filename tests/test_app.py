"""Workflow, maker-checker, and audit-trail tests through the HTTP API (SQLite in memory)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from faktura.app import service
from faktura.app.api import app, load_context
from faktura.app.auth import DevHeaderAuth
from faktura.app.db import Base, make_engine, make_sessionmaker
from faktura.evals.extraction import load_labels
from faktura.evals.matching_eval import perfect_reading
from faktura.learn.misread import Correction, MisreadModel
from faktura.synth.cli import write_dataset
from faktura.synth.config import SynthConfig
from faktura.synth.generate import generate

STAFF = {"X-User": "budi", "X-Role": "ap_staff"}
STAFF2 = {"X-User": "dewi", "X-Role": "ap_staff"}
APPROVER = {"X-User": "sari", "X-Role": "approver"}
BUDI_AS_APPROVER = {"X-User": "budi", "X-Role": "approver"}


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    cfg = SynthConfig(seed=41, n_purchase_orders=40, anomaly_rate=0.4)
    out = tmp_path_factory.mktemp("app")
    write_dataset(generate(cfg), cfg, out, render=True)
    return out


@pytest.fixture()
def env(data_dir):
    """Fresh DB per test, seeded with perfect readings except where a test overrides."""
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    app.state.sessions = make_sessionmaker(engine)
    app.state.ctx = load_context(data_dir)
    app.state.auth = DevHeaderAuth()
    labels = load_labels(data_dir)
    with TestClient(app, headers=STAFF) as client:
        yield client, labels, data_dir
    engine.dispose()


def _ingest(env, lb, **overrides):
    _, _, data_dir = env
    reading = perfect_reading(lb.invoice).model_copy(update=overrides).model_dump(mode="json")
    with app.state.sessions() as s:
        row = service.ingest(
            s,
            app.state.ctx,
            lb.invoice_id,
            str(data_dir / "invoices" / f"{lb.invoice_id}.pdf"),
            reading,
        )
        s.commit()
        return row.id


def _clean(labels):
    return [lb for lb in labels if not lb.anomalies]


def test_clean_invoice_is_auto_approved_with_audit_trail(env):
    client, labels, _ = env
    inv_id = _ingest(env, _clean(labels)[0])
    body = client.get(f"/invoices/{inv_id}").json()
    assert body["state"] == "approved" and body["auto_approved"]
    actions = [e["action"] for e in body["audit"]]
    assert actions == ["received", "read", "decided", "state_changed", "erp_queued"]


def test_misread_total_corrected_then_needs_a_different_checker(env):
    client, labels, _ = env
    lb = _clean(labels)[0]
    inv_id = _ingest(env, lb, total=lb.invoice.total + 1000)  # arithmetic breaks
    assert client.get(f"/invoices/{inv_id}").json()["state"] == "needs_action"

    r = client.post(
        f"/invoices/{inv_id}/corrections",
        headers=STAFF,
        json={"field": "total", "value": lb.invoice.total, "note": "read from page"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "awaiting_approval"  # engine passes it, but a human touched it

    # The corrector cannot approve, even with an approver role.
    r = client.post(f"/invoices/{inv_id}/approve", headers=BUDI_AS_APPROVER, json={})
    assert r.status_code == 403 and "maker-checker" in r.json()["detail"]

    r = client.post(f"/invoices/{inv_id}/approve", headers=APPROVER, json={"note": "ok"})
    assert r.status_code == 200 and r.json()["state"] == "approved"
    detail = client.get(f"/invoices/{inv_id}").json()
    assert detail["model_reading"]["total"] == lb.invoice.total + 1000  # kept for audit
    assert "field_corrected" in [e["action"] for e in detail["audit"]]


def test_roles_are_enforced(env):
    client, labels, _ = env
    lb = _clean(labels)[0]
    inv_id = _ingest(env, lb, total=lb.invoice.total + 1)
    r = client.post(
        f"/invoices/{inv_id}/corrections",
        headers=APPROVER,
        json={"field": "total", "value": lb.invoice.total},
    )
    assert r.status_code == 403
    r = client.post(f"/invoices/{inv_id}/approve", headers=STAFF, json={})
    assert r.status_code == 403
    r = client.get("/invoices", headers={"X-User": "x", "X-Role": "admin"})
    assert r.status_code == 403  # reads need a Faktura role too


def test_fraud_hold_needs_vendor_verification(env):
    client, labels, _ = env
    lb = _clean(labels)[0]
    inv_id = _ingest(env, lb, bank_account="9" * 10)
    assert client.get(f"/invoices/{inv_id}").json()["decision"] == "fraud_hold"

    r = client.post(
        f"/invoices/{inv_id}/submit",
        headers=STAFF,
        json={"justification": "vendor says new account"},
    )
    assert r.status_code == 409
    r = client.post(
        f"/invoices/{inv_id}/submit",
        headers=STAFF,
        json={
            "justification": "vendor moved banks, confirmed by phone",
            "vendor_verification": {
                "contact_name": "Pak Andi",
                "contact_channel": "phone from vendor master",
                "confirmed_details": "new account",
            },
        },
    )
    assert r.status_code == 200 and r.json()["state"] == "awaiting_approval"
    r = client.post(f"/invoices/{inv_id}/approve", headers=APPROVER, json={})
    assert r.status_code == 200 and r.json()["state"] == "approved"


def test_wrong_state_stale_version_and_bad_value(env):
    client, labels, _ = env
    lb = _clean(labels)[0]
    inv_id = _ingest(env, lb, total=lb.invoice.total + 1)
    assert client.post(f"/invoices/{inv_id}/approve", headers=APPROVER, json={}).status_code == 409
    r = client.post(
        f"/invoices/{inv_id}/corrections",
        headers=STAFF,
        json={"field": "total", "value": lb.invoice.total, "version": 99},
    )
    assert r.status_code == 409 and "reload" in r.json()["detail"]
    r = client.post(
        f"/invoices/{inv_id}/corrections",
        headers=STAFF,
        json={"field": "total", "value": "seratus ribu"},
    )
    assert r.status_code == 422
    r = client.post(
        f"/invoices/{inv_id}/corrections", headers=STAFF, json={"field": "not_a_field", "value": 1}
    )
    assert r.status_code == 422


def test_rejected_invoice_is_closed(env):
    client, labels, _ = env
    lb = _clean(labels)[0]
    inv_id = _ingest(env, lb, bank_account="9" * 10)
    r = client.post(f"/invoices/{inv_id}/reject", headers=STAFF2, json={"reason": "fake invoice"})
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    r = client.post(f"/invoices/{inv_id}/submit", headers=STAFF, json={"justification": "x" * 20})
    assert r.status_code == 409


def test_duplicate_is_held_across_requests(env):
    _, labels, _ = env
    lb = _clean(labels)[0]
    _ingest(env, lb)
    with app.state.sessions() as s:
        row = service.ingest(
            s,
            app.state.ctx,
            "resubmitted",
            "x.pdf",
            perfect_reading(lb.invoice).model_dump(mode="json"),
        )
        s.commit()
        assert row.decision == "fraud_hold"
        assert any(r["code"].startswith("duplicate") for r in row.reasons)


def test_audit_chain_detects_tampering(env):
    client, labels, _ = env
    for lb in _clean(labels)[:3]:
        _ingest(env, lb)
    assert client.get("/audit/verify").json() == {
        "ok": True,
        "events": 15,
        "first_broken_seq": None,
    }
    with app.state.sessions() as s:  # an "insider" edits history directly in the DB
        s.execute(text("UPDATE audit_events SET actor = 'mallory' WHERE seq = 5"))
        s.commit()
    assert client.get("/audit/verify").json() == {"ok": False, "events": 5, "first_broken_seq": 5}


def test_audit_chain_detects_deleted_tail(env):
    client, labels, _ = env
    _ingest(env, _clean(labels)[0])
    with app.state.sessions() as s:
        s.execute(text("DELETE FROM audit_events WHERE seq = (SELECT max(seq) FROM audit_events)"))
        s.commit()
    assert client.get("/audit/verify").json()["ok"] is False


def test_image_list_and_summary(env):
    client, labels, _ = env
    for lb in labels[:4]:
        _ingest(env, lb)
    inv_id = labels[0].invoice_id
    r = client.get(f"/invoices/{inv_id}/image")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(client.get("/invoices").json()["items"]) == 4
    groups = client.get("/summary").json()["groups"]
    assert sum(g["count"] for g in groups) == 4


def _one_digit_misread(value: str) -> str:
    i = next(i for i, ch in enumerate(value) if ch == "6")
    return value[:i] + "0" + value[i + 1 :]


def test_learned_misread_is_checked_on_the_page_then_held_or_cleared(env):
    """Triage from the misread model: confirm-as-printed brings the hold back; a fix clears it."""
    client, labels, _ = env
    app.state.ctx.misread = MisreadModel.fit([Correction("bank_account", "102", "162")] * 3)
    clean = [lb for lb in _clean(labels) if "6" in lb.invoice.bank_account]
    first, second = clean[0], clean[1]

    a = _ingest(env, first, bank_account=_one_digit_misread(first.invoice.bank_account))
    body = client.get(f"/invoices/{a}").json()
    assert body["decision"] == "review_reading"
    assert "probable_misread" in body["reason_codes"]
    decided = [e for e in body["audit"] if e["action"] == "decided"][-1]
    assert decided["payload"]["misread_model"] == app.state.ctx.misread.version
    # It cannot skip the page check and reach an approver without vendor verification.
    r = client.post(
        f"/invoices/{a}/submit", headers=STAFF, json={"justification": "looks fine to me, pay it"}
    )
    assert r.status_code == 409 and "against the page" in r.json()["detail"]
    # The clerk reads the page and finds the digits really are as read: held.
    r = client.post(f"/invoices/{a}/confirmations", headers=STAFF, json={"field": "bank_account"})
    assert r.json()["decision"] == "fraud_hold"

    b = _ingest(env, second, bank_account=_one_digit_misread(second.invoice.bank_account))
    r = client.post(
        f"/invoices/{b}/corrections",
        headers=STAFF,
        json={"field": "bank_account", "value": second.invoice.bank_account},
    )
    assert r.json()["state"] == "awaiting_approval"  # a person touched it: maker-checker
