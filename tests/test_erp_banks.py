"""Writing approved bank changes to Odoo: idempotent steps, ordering before bills."""

import pytest

from faktura.app.db import (
    AuditEvent,
    BankChangeRow,
    Base,
    ErpOutbox,
    InvoiceRow,
    make_engine,
    make_sessionmaker,
)
from faktura.app.erp_sync import drain
from faktura.erp.odoo import OdooError
from faktura.erp.odoo_banks import apply_bank_change
from faktura.erp.odoo_bills import NeedsAttention

CHANGE = {
    "vendor_id": "V001",
    "new": {"bank_name": "Bank Baru", "bank_account": "7712345678"},
}


class FakeOdoo:
    """Just enough of res.partner / res.partner.bank."""

    def __init__(self):
        self.partners = [{"id": 1, "ref": "V001", "name": "PT Contoh"}]
        self.banks = [
            {"id": 10, "partner_id": 1, "account_number": "1234567890", "allow_out_payment": True}
        ]
        self.calls = []

    def search_read(self, model, domain, fields, **_):
        rows = self.partners if model == "res.partner" else self.banks
        out = []
        for r in rows:
            ok = all(
                (r.get(k) == v if op == "=" else True)
                for k, op, v in domain
                if k in ("ref", "partner_id")
            )
            if ok:
                out.append(dict(r))
        return out

    def call(self, model, method, ids=None, **kw):
        self.calls.append((method, ids))
        if method == "create":
            vals = kw["vals_list"][0]
            new_id = 10 + len(self.banks)
            self.banks.append({"id": new_id, "allow_out_payment": False, **vals})
            return [new_id]
        if method == "write":
            for b in self.banks:
                if b["id"] in ids:
                    b.update(kw["vals"])
            return True
        raise AssertionError(method)


def test_new_account_becomes_the_only_trusted_one_and_rerun_is_a_no_op():
    odoo = FakeOdoo()
    bank_id = apply_bank_change(odoo, CHANGE)
    trusted = [b for b in odoo.banks if b["allow_out_payment"]]
    assert [b["id"] for b in trusted] == [bank_id]
    assert trusted[0]["account_number"] == "7712345678"
    n = len(odoo.calls)
    assert apply_bank_change(odoo, CHANGE) == bank_id
    assert len(odoo.calls) == n  # nothing left to do


def test_unknown_vendor_needs_attention():
    odoo = FakeOdoo()
    with pytest.raises(NeedsAttention):
        apply_bank_change(odoo, CHANGE | {"vendor_id": "V999"})


@pytest.fixture
def sessions():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = make_sessionmaker(engine)
    with s() as session:
        session.add(
            BankChangeRow(
                vendor_id="V001",
                status="approved",
                old_bank_name="Bank Lama",
                old_bank_account="1234567890",
                new_bank_name="Bank Baru",
                new_bank_account="7712345678",
                channel="letter",
                requested_by="budi",
                erp_status="pending",
                created_at="t",
                updated_at="t",
            )
        )
        session.commit()
    return s


def test_drain_syncs_bank_changes_before_bills(sessions):
    order = []

    def apply(change):
        order.append("bank")
        return 42

    def post(invoice_id, payload):
        order.append("bill")
        raise AssertionError("no bills queued")

    assert drain(sessions, post, apply) == {"bank_synced": 1}
    with sessions() as s:
        row = s.get(BankChangeRow, 1)
        assert row.erp_status == "synced"
        assert s.query(AuditEvent).one().action == "bank_change_synced"
    assert drain(sessions, post, apply) == {}  # done, not repeated


def test_bill_into_an_unsynced_account_waits_instead_of_failing(sessions):
    with sessions() as s:
        s.add(
            InvoiceRow(
                id="inv-1",
                seq=1,
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
                auto_approved=False,
                version=1,
            )
        )
        s.add(
            ErpOutbox(
                invoice_id="inv-1",
                payload={"vendor_id": "V001", "reading": {"bank_account": "7712-345-678"}},
                status="pending",
                attempts=0,
                next_attempt_at="2026-01-01T00:00:00+00:00",
                created_at="t",
                updated_at="t",
            )
        )
        s.commit()

    def post(invoice_id, payload):
        raise AssertionError("must wait for the bank change")

    def refuse(change):
        raise OdooError(403, "AccessError", "not allowed")

    assert drain(sessions, post, refuse) == {"bank_needs_attention": 1, "pending": 1}
    with sessions() as s:
        row = s.query(ErpOutbox).one()
        assert row.attempts == 0 and "waiting for bank change 1" in row.last_error


def test_retryable_error_waits_for_the_next_pass(sessions):
    def apply(change):
        raise OdooError(503, "ServiceUnavailable", "down")

    assert drain(sessions, lambda *_: None, apply) == {"bank_pending": 1}
    with sessions() as s:
        assert s.get(BankChangeRow, 1).erp_status == "pending"

    def refuse(change):
        raise OdooError(403, "AccessError", "not allowed to validate bank accounts")

    assert drain(sessions, lambda *_: None, refuse) == {"bank_needs_attention": 1}
