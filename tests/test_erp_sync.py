import datetime as dt
import json

import httpx
import pytest
from sqlalchemy import select

from faktura.app.db import (
    AuditEvent,
    Base,
    ErpOutbox,
    InvoiceRow,
    make_engine,
    make_sessionmaker,
)
from faktura.app.erp_sync import MAX_ATTEMPTS, drain, process_one
from faktura.erp.odoo import OdooClient, OdooError
from faktura.erp.odoo_bills import NeedsAttention, PostedBill, match_invoice_amounts
from faktura.extract.schema import ExtractedInvoice

T0 = dt.datetime(2026, 1, 1, 8, 0, tzinfo=dt.UTC)  # in the past: rows are due for drain()


@pytest.fixture
def sessions():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = make_sessionmaker(engine)
    with s() as session:
        for i in (1, 2):
            session.add(
                InvoiceRow(
                    id=f"inv-{i}",
                    seq=i,
                    pdf_path="x.pdf",
                    state="approved",
                    decision="approve",
                    reasons=[],
                    reading={},
                    model_reading={},
                    human_verified=[],
                    makers=[],
                    vendor_id="V001",
                    total=1,
                    auto_approved=True,
                    version=1,
                )
            )
            session.add(
                ErpOutbox(
                    invoice_id=f"inv-{i}",
                    payload={"n": i},
                    status="pending",
                    attempts=0,
                    next_attempt_at=T0.isoformat(timespec="seconds"),
                    created_at="t",
                    updated_at="t",
                )
            )
        session.commit()
    return s


def outbox(sessions):
    with sessions() as s:
        return {r.invoice_id: r for r in s.execute(select(ErpOutbox)).scalars()}


def actions(sessions, invoice_id):
    with sessions() as s:
        q = select(AuditEvent.action).where(AuditEvent.invoice_id == invoice_id)
        return list(s.execute(q).scalars())


def test_posts_each_due_row_once_and_records_it(sessions):
    posted = []

    def post(invoice_id, payload):
        posted.append(invoice_id)
        return PostedBill(move_id=10 + payload["n"], name=f"BILL/{payload['n']}", created=True)

    assert drain(sessions, post) == {"posted": 2}
    assert drain(sessions, post) == {}  # nothing left: never posted twice
    assert posted == ["inv-1", "inv-2"]
    assert (outbox(sessions)["inv-2"].erp_name, outbox(sessions)["inv-2"].erp_id) == ("BILL/2", 12)
    assert "erp_bill_posted" in actions(sessions, "inv-1")


def test_erp_refusal_goes_to_a_person_not_into_a_retry_loop(sessions):
    def post(invoice_id, payload):
        raise NeedsAttention("bank account not trusted")

    with sessions() as s:
        assert process_one(s, post, now=T0) == "needs_attention"
    row = outbox(sessions)["inv-1"]
    assert row.last_error == "bank account not trusted" and row.attempts == 1
    assert "erp_needs_attention" in actions(sessions, "inv-1")


def test_outage_backs_off_then_gives_up(sessions):
    def post(invoice_id, payload):
        raise httpx.ConnectError("odoo down")

    with sessions() as s:
        assert process_one(s, post, now=T0) == "pending"
    row = outbox(sessions)["inv-1"]
    assert row.next_attempt_at == (T0 + dt.timedelta(seconds=15)).isoformat(timespec="seconds")
    with sessions() as s:  # inv-1 is not due yet, so inv-2 is next
        process_one(s, post, now=T0)
    assert outbox(sessions)["inv-2"].attempts == 1 and outbox(sessions)["inv-1"].attempts == 1

    now = T0
    for _ in range(MAX_ATTEMPTS):
        now += dt.timedelta(hours=2)
        with sessions() as s:
            process_one(s, post, now=now)
            process_one(s, post, now=now)
    assert {r.status for r in outbox(sessions).values()} == {"needs_attention"}


def test_validation_error_from_erp_is_not_retried(sessions):
    def post(invoice_id, payload):
        raise OdooError(422, "odoo.exceptions.ValidationError", "bad partner")

    with sessions() as s:
        assert process_one(s, post, now=T0) == "needs_attention"


# ---------------------------------------------------------------- amounts vs. Odoo


def reading(dpp=12345, vat=1357, total=13702):
    return ExtractedInvoice.model_validate(
        {
            "invoice_number": "A/1",
            "tax_invoice_number": "1" * 16,
            "invoice_date": "2025-07-01",
            "po_number": "PO/1",
            "vendor_name": "PT A",
            "vendor_npwp": "0" * 16,
            "bank_name": "BNI",
            "bank_account": "123",
            "lines": [{"description": "x", "qty": 1, "unit_price": dpp, "amount": dpp}],
            "dpp": dpp,
            "vat": vat,
            "total": total,
            "uncertain_fields": [],
        }
    )


def odoo_move(tax, total):
    return {
        "id": 5,
        "name": "/",
        "state": "draft",
        "amount_untaxed": 12345.0,
        "amount_tax": tax,
        "amount_total": total,
        "tax_totals": {"subtotals": [{"tax_groups": [{"id": 1, "tax_amount_currency": tax}]}]},
    }


def test_sen_rounding_gap_is_closed_by_setting_the_printed_vat():
    writes = []

    def handler(req):
        body = json.loads(req.content)
        if req.url.path.endswith("/write"):
            writes.append(body["vals"]["tax_totals"]["subtotals"][0]["tax_groups"][0])
            return httpx.Response(200, json=True)
        return httpx.Response(200, json=[odoo_move(1357.0, 13702.0)])

    client = OdooClient("http://o", "k", transport=httpx.MockTransport(handler))
    move = match_invoice_amounts(client, odoo_move(1357.95, 13702.95), reading())
    assert writes == [{"id": 1, "tax_amount_currency": 1357}]
    assert move["amount_total"] == 13702.0


def test_a_real_vat_difference_is_never_adjusted():
    def handler(req):
        raise AssertionError("must not write")

    client = OdooClient("http://o", "k", transport=httpx.MockTransport(handler))
    with pytest.raises(NeedsAttention):
        match_invoice_amounts(client, odoo_move(1357.95, 13702.95), reading(vat=1362, total=13707))
