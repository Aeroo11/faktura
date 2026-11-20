"""Vendor bank-account changes: the only way a new payee account is accepted.

The classic AP fraud (business email compromise) is an invoice or a letter that says
"we have a new bank account". The controls here are the standard ones:

1. **Request.** AP staff record the new account and how the request arrived. From an
   invoice, the account must first have been checked against the page, so a misread is
   never "verified" as a change.
2. **Call-back.** A different person calls the vendor on the number already on file in
   the vendor master. Faktura records that number itself; nobody types it, so a number
   printed on the invoice or in the email cannot be used. If the vendor denies the
   change, the request is rejected and every open invoice asking to be paid into that
   account is rejected with it.
3. **Approval.** An approver who neither recorded the request nor made the call decides.
   Once approved, the new account replaces the old one in the master data Faktura
   decides with, open invoices from that vendor are decided again, and in Odoo mode the
   account is written to the ERP as trusted (faktura-erp-sync).

An invoice held for a changed bank account cannot be submitted for payment on its own
(service.submit); it is released only by an approved change.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from faktura.app import audit, service
from faktura.app.db import BankChangeRow, BankChangeStatus, InvoiceRow, State
from faktura.app.service import (
    AP_STAFF,
    APPROVER,
    SYSTEM,
    Context,
    Forbidden,
    InvalidValue,
    NotFound,
    User,
    WorkflowError,
)
from faktura.matching import Status

OPEN = (BankChangeStatus.AWAITING_CALLBACK, BankChangeStatus.AWAITING_APPROVAL)
OUTCOMES = ("confirmed", "denied")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _get(session: Session, change_id: int) -> BankChangeRow:
    row = session.execute(
        select(BankChangeRow).where(BankChangeRow.id == change_id).with_for_update()
    ).scalar()
    if row is None:
        raise NotFound(f"bank change {change_id}")
    return row


def _check(row: BankChangeRow, status: BankChangeStatus, expected_version: int | None) -> None:
    if row.status != status:
        raise WorkflowError(f"bank change {row.id} is {row.status}; action needs {status.value}")
    if expected_version is not None and row.version != expected_version:
        raise WorkflowError(
            f"bank change {row.id} changed (version {row.version}, you had {expected_version}); "
            "reload"
        )


def _event(session: Session, row: BankChangeRow, actor: str, action: str, **payload: Any) -> None:
    audit.append(
        session,
        row.source_invoice_id,
        actor,
        action,
        {"bank_change_id": row.id, "vendor_id": row.vendor_id, **payload},
    )


def open_request(session: Session, vendor_id: str) -> BankChangeRow | None:
    return session.execute(
        select(BankChangeRow).where(
            BankChangeRow.vendor_id == vendor_id, BankChangeRow.status.in_(OPEN)
        )
    ).scalar()


# ------------------------------------------------------------------ actions


def request_change(
    session: Session,
    ctx: Context,
    user: User,
    channel: str,
    source_invoice_id: str | None = None,
    vendor_id: str | None = None,
    new_bank_name: str | None = None,
    new_bank_account: str | None = None,
) -> BankChangeRow:
    """Record a request. From an invoice, the new account is the one printed on it."""
    service._require(user, AP_STAFF)
    if len(channel.strip()) < 5:
        raise InvalidValue("say how the request arrived, e.g. 'printed on invoice INV/123'")
    if source_invoice_id is not None:
        inv = service._get(session, source_invoice_id)
        service._require_state(inv, State.NEEDS_ACTION)
        bank_reasons = [r for r in inv.reasons if r["code"] in service.BANK_REASONS]
        if not bank_reasons:
            raise WorkflowError(f"invoice {inv.id} is not held for a changed bank account")
        unchecked = sorted({f for r in bank_reasons for f in r["fields"]} - set(inv.human_verified))
        if unchecked:
            raise WorkflowError(
                f"check {', '.join(unchecked)} against the page first (correct or confirm it): "
                "a misread must never become a bank change"
            )
        vendor_id = inv.vendor_id
        new_bank_name = inv.reading.get("bank_name")
        new_bank_account = inv.reading.get("bank_account")
    if not vendor_id:
        raise InvalidValue("which vendor? (the invoice's vendor could not be identified)")

    vendor = service.master_snapshot(session, ctx).vendors.get(vendor_id)
    if vendor is None:
        raise InvalidValue(f"unknown vendor {vendor_id!r}")
    account = digits(new_bank_account)
    name = (new_bank_name or "").strip()
    if not 6 <= len(account) <= 20 or not name:
        raise InvalidValue("the new account needs a bank name and a 6-20 digit account number")
    if account == vendor.bank_account and name.casefold() == vendor.bank_name.casefold():
        raise InvalidValue("that is already the account on file")
    existing = open_request(session, vendor_id)
    if existing is not None:
        raise WorkflowError(f"bank change {existing.id} is already open for this vendor")

    now = _now()
    row = BankChangeRow(
        vendor_id=vendor_id,
        status=BankChangeStatus.AWAITING_CALLBACK.value,
        old_bank_name=vendor.bank_name,
        old_bank_account=vendor.bank_account,
        new_bank_name=name,
        new_bank_account=account,
        source_invoice_id=source_invoice_id,
        channel=channel.strip(),
        requested_by=user.name,
        callback=None,
        created_at=now,
        updated_at=now,
        version=1,
    )
    session.add(row)
    session.flush()
    _event(
        session,
        row,
        user.name,
        "bank_change_requested",
        old={"bank_name": row.old_bank_name, "bank_account": row.old_bank_account},
        new={"bank_name": row.new_bank_name, "bank_account": row.new_bank_account},
        channel=row.channel,
    )
    return row


def record_callback(
    session: Session,
    ctx: Context,
    user: User,
    change_id: int,
    spoke_with: str,
    outcome: str,
    note: str = "",
    expected_version: int | None = None,
) -> BankChangeRow:
    """The vendor, called on the number on file, confirmed or denied the change."""
    service._require(user, AP_STAFF)
    row = _get(session, change_id)
    _check(row, BankChangeStatus.AWAITING_CALLBACK, expected_version)
    if user.name == row.requested_by:
        raise Forbidden("the call-back must be made by someone other than whoever recorded it")
    if outcome not in OUTCOMES:
        raise InvalidValue(f"outcome must be one of {', '.join(OUTCOMES)}")
    if len(spoke_with.strip()) < 2:
        raise InvalidValue("who did you speak to at the vendor?")
    vendor = ctx.master.snapshot().vendors.get(row.vendor_id)
    phone = vendor.phone if vendor else ""
    if not phone:
        raise WorkflowError(
            "the vendor has no phone number on file. Fix the vendor master first; never call "
            "a number from the invoice or the request"
        )
    row.callback = {
        "called_by": user.name,
        "phone": phone,  # taken from the master, not typed
        "spoke_with": spoke_with.strip(),
        "outcome": outcome,
        "note": note.strip(),
        "at": _now(),
    }
    _event(session, row, user.name, "bank_change_callback", **row.callback)
    if outcome == "denied":
        row.status = BankChangeStatus.REJECTED.value
        row.decided_by = user.name
        row.decision_note = "vendor denied the change on the call-back"
        _reject_invoices_paying(session, row, user.name)
    else:
        row.status = BankChangeStatus.AWAITING_APPROVAL.value
    row.updated_at = _now()
    row.version += 1
    return row


def decide(
    session: Session,
    ctx: Context,
    user: User,
    change_id: int,
    approve: bool,
    note: str = "",
    expected_version: int | None = None,
) -> BankChangeRow:
    service._require(user, APPROVER)
    row = _get(session, change_id)
    _check(row, BankChangeStatus.AWAITING_APPROVAL, expected_version)
    involved = {row.requested_by, (row.callback or {}).get("called_by")}
    if user.name in involved:
        raise Forbidden(f"{user.name!r} recorded or verified this change and cannot decide it")
    if not approve and len(note.strip()) < 5:
        raise InvalidValue("a rejection needs a reason")
    current = service.master_snapshot(session, ctx).vendors.get(row.vendor_id)
    if approve and (current is None or current.bank_account != row.old_bank_account):
        raise WorkflowError(
            "the vendor's account on file changed since this request was made; reject it and "
            "record a new request"
        )
    row.decided_by = user.name
    row.decision_note = note.strip()
    row.updated_at = _now()
    row.version += 1
    if not approve:
        row.status = BankChangeStatus.REJECTED.value
        _event(session, row, user.name, "bank_change_rejected", note=row.decision_note)
        return row
    row.status = BankChangeStatus.APPROVED.value
    row.erp_status = "pending" if ctx.master.name == "odoo" else None
    _event(session, row, user.name, "bank_change_approved", note=row.decision_note)
    _redecide_vendor_invoices(session, ctx, row, user)
    return row


# ------------------------------------------------------------------ effects on invoices


def _open_invoices(session: Session, vendor_id: str, *states: State) -> list[InvoiceRow]:
    return list(
        session.execute(
            select(InvoiceRow)
            .where(InvoiceRow.vendor_id == vendor_id, InvoiceRow.state.in_(states))
            .order_by(InvoiceRow.seq)
            .with_for_update()
        ).scalars()
    )


def _reject_invoices_paying(session: Session, change: BankChangeRow, actor: str) -> None:
    """The vendor says this is not their account: nothing open may be paid into it."""
    for inv in _open_invoices(
        session, change.vendor_id, State.NEEDS_ACTION, State.AWAITING_APPROVAL
    ):
        if digits(inv.reading.get("bank_account")) != change.new_bank_account:
            continue
        audit.append(
            session,
            inv.id,
            actor,
            "rejected",
            {
                "reason": (
                    f"vendor denied this bank account on a call-back to the number on file "
                    f"(bank change {change.id}): likely payment fraud"
                ),
                "bank_change_id": change.id,
            },
        )
        service._set_state(session, inv, State.REJECTED, actor)
        inv.version += 1


def _redecide_vendor_invoices(
    session: Session, ctx: Context, change: BankChangeRow, approver: User
) -> None:
    """Open invoices from this vendor are decided again against the new account.

    One that now passes still needs an approver, and not the one who approved the
    change: the person who changed the master must not also release the payment.
    """
    for inv in _open_invoices(session, change.vendor_id, State.NEEDS_ACTION):
        if approver.name not in inv.makers:
            inv.makers = [*inv.makers, approver.name]
        service._evaluate(session, ctx, inv)
        if inv.decision == Status.APPROVE:
            service._set_state(session, inv, State.AWAITING_APPROVAL, SYSTEM)
        inv.version += 1


# ------------------------------------------------------------------ queries


def view(row: BankChangeRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "vendor_id": row.vendor_id,
        "status": row.status,
        "old": {"bank_name": row.old_bank_name, "bank_account": row.old_bank_account},
        "new": {"bank_name": row.new_bank_name, "bank_account": row.new_bank_account},
        "source_invoice_id": row.source_invoice_id,
        "channel": row.channel,
        "requested_by": row.requested_by,
        "callback": row.callback,
        "decided_by": row.decided_by,
        "decision_note": row.decision_note,
        "erp_status": row.erp_status,
        "erp_error": row.erp_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "version": row.version,
    }
