"""Workflow rules. The HTTP layer only translates; every rule is enforced here.

Life of an invoice:
    ingest -> engine decides -> approve       => APPROVED (auto, actor "system")
                             -> anything else => NEEDS_ACTION
    NEEDS_ACTION: AP staff (maker) corrects or confirms fields; the engine re-decides
                  after every change. A changed invoice that now passes goes to
                  AWAITING_APPROVAL. Anything a person touched is never paid on their
                  word alone. The maker can also submit an exception or a fraud hold
                  with a justification; a fraud hold also needs a recorded vendor
                  verification (call-back to a known contact).
    AWAITING_APPROVAL: an approver who is not one of the invoice's makers approves.
    Either role can reject while the invoice is open.

Every change and its audit event are written in the same transaction.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from faktura.app import audit
from faktura.app.db import InvoiceRow, State
from faktura.extract.schema import ExtractedInvoice, FieldName
from faktura.master import MasterData
from faktura.matching import Ledger, Status, add_billing, decide
from faktura.validate import resolve_vendor

AP_STAFF = "ap_staff"
APPROVER = "approver"
ROLES = {AP_STAFF, APPROVER}
SYSTEM = "system"


class WorkflowError(Exception):
    """The action is not allowed in the invoice's current state (HTTP 409)."""


class Forbidden(Exception):
    """The user may not perform this action (HTTP 403)."""


class NotFound(Exception):
    """No such invoice (HTTP 404)."""


class InvalidValue(Exception):
    """A corrected value does not fit the field (HTTP 422)."""


@dataclass(frozen=True)
class User:
    name: str
    role: str


@dataclass
class Context:
    md: MasterData
    vat_bps: int


class VendorVerification(BaseModel):
    """Proof that changed payment details were confirmed outside the invoice itself."""

    contact_name: str = Field(min_length=2)
    contact_channel: str = Field(min_length=2, description="e.g. phone number from master")
    confirmed_details: str = Field(min_length=2, description="what the vendor confirmed")


# ------------------------------------------------------------------ helpers


def _get(session: Session, invoice_id: str, lock: bool = True) -> InvoiceRow:
    stmt = select(InvoiceRow).where(InvoiceRow.id == invoice_id)
    if lock:
        stmt = stmt.with_for_update()
    row = session.execute(stmt).scalar()
    if row is None:
        raise NotFound(invoice_id)
    return row


def _require(user: User, role: str) -> None:
    if user.role != role:
        raise Forbidden(f"requires role {role!r}, user {user.name!r} is {user.role!r}")


def _require_state(row: InvoiceRow, *states: State) -> None:
    if row.state not in states:
        allowed = ", ".join(s.value for s in states)
        raise WorkflowError(f"invoice {row.id} is {row.state}; action needs {allowed}")


def _check_version(row: InvoiceRow, expected: int | None) -> None:
    if expected is not None and row.version != expected:
        raise WorkflowError(
            f"invoice {row.id} changed (version {row.version}, you had {expected}); reload"
        )


def _effective_reading(row: InvoiceRow) -> ExtractedInvoice:
    """Current reading; fields a person verified are no longer 'uncertain'."""
    ext = ExtractedInvoice.model_validate(row.reading)
    verified = set(row.human_verified)
    return ext.model_copy(
        update={"uncertain_fields": [f for f in ext.uncertain_fields if f.value not in verified]}
    )


def build_ledger(session: Session, before_seq: int, md: MasterData) -> Ledger:
    """What AP had received before this invoice (for duplicates and cumulative billing)."""
    ledger = Ledger()
    earlier = session.execute(
        select(InvoiceRow).where(InvoiceRow.seq < before_seq).order_by(InvoiceRow.seq)
    ).scalars()
    for r in earlier:
        ext = ExtractedInvoice.model_validate(r.reading)
        vendor, _ = resolve_vendor(ext, md)
        if r.state != State.REJECTED and not any(
            x["code"].startswith("duplicate") for x in r.reasons
        ):
            add_billing(ext, md, ledger)
        ledger.record(r.id, vendor.vendor_id if vendor else None, ext)
    return ledger


def _approved_numbers(session: Session, vendor_id: str | None, exclude: str) -> list[str]:
    if vendor_id is None:
        return []
    rows = session.execute(
        select(InvoiceRow.reading).where(
            InvoiceRow.vendor_id == vendor_id,
            InvoiceRow.state == State.APPROVED,
            InvoiceRow.id != exclude,
        )
    ).scalars()
    return [r["invoice_number"] for r in rows if r.get("invoice_number")]


def _evaluate(session: Session, ctx: Context, row: InvoiceRow) -> None:
    ext = _effective_reading(row)
    vendor, _ = resolve_vendor(ext, ctx.md)
    vid = vendor.vendor_id if vendor else None
    # Per-call copy: requests run in a thread pool and must not share this history.
    md = copy.copy(ctx.md)
    md.approved_invoice_numbers = {vid: _approved_numbers(session, vid, row.id)} if vid else {}
    d = decide(row.id, ext, md, build_ledger(session, row.seq, md), ctx.vat_bps)
    row.decision = d.status.value
    row.reasons = [r.model_dump(mode="json") for r in d.reasons]
    row.vendor_id = d.vendor_id
    row.total = ext.total
    audit.append(
        session, row.id, SYSTEM, "decided", {"decision": row.decision, "reasons": row.reasons}
    )


def _set_state(session: Session, row: InvoiceRow, state: State, actor: str) -> None:
    if row.state != state:
        audit.append(
            session, row.id, actor, "state_changed", {"from": row.state, "to": state.value}
        )
        row.state = state.value


def _after_human_change(session: Session, ctx: Context, row: InvoiceRow, user: User) -> None:
    if user.name not in row.makers:
        row.makers = [*row.makers, user.name]
    _evaluate(session, ctx, row)
    if row.decision == Status.APPROVE:
        _set_state(session, row, State.AWAITING_APPROVAL, SYSTEM)
    row.version += 1


# ------------------------------------------------------------------ actions


def ingest(
    session: Session, ctx: Context, invoice_id: str, pdf_path: str, model_reading: dict[str, Any]
) -> InvoiceRow:
    ExtractedInvoice.model_validate(model_reading)
    seq = (session.execute(select(func.max(InvoiceRow.seq))).scalar() or 0) + 1
    row = InvoiceRow(
        id=invoice_id,
        seq=seq,
        pdf_path=pdf_path,
        state=State.NEEDS_ACTION.value,
        decision=Status.REVIEW.value,
        reasons=[],
        reading=model_reading,
        model_reading=model_reading,
        human_verified=[],
        makers=[],
        vendor_id=None,
        total=None,
        auto_approved=False,
        version=1,
    )
    session.add(row)
    session.flush()
    audit.append(session, row.id, SYSTEM, "received", {"pdf_path": pdf_path, "seq": seq})
    audit.append(session, row.id, SYSTEM, "read", {"reading": model_reading})
    _evaluate(session, ctx, row)
    if row.decision == Status.APPROVE:
        row.auto_approved = True
        _set_state(session, row, State.APPROVED, SYSTEM)
    return row


def correct_field(
    session: Session,
    ctx: Context,
    user: User,
    invoice_id: str,
    field: str,
    value: Any,
    note: str,
    expected_version: int | None = None,
) -> InvoiceRow:
    _require(user, AP_STAFF)
    row = _get(session, invoice_id)
    _require_state(row, State.NEEDS_ACTION)
    _check_version(row, expected_version)
    try:
        name = FieldName(field)
        new = ExtractedInvoice.model_validate(row.reading | {name.value: value})
    except (ValueError, ValidationError) as e:
        raise InvalidValue(str(e)) from e
    old = row.reading.get(name.value)
    row.reading = new.model_dump(mode="json")
    if name.value not in row.human_verified:
        row.human_verified = [*row.human_verified, name.value]
    audit.append(
        session,
        row.id,
        user.name,
        "field_corrected",
        {
            "field": name.value,
            "old": old,
            "new": row.reading[name.value],
            "model_read": row.model_reading.get(name.value),
            "note": note,
        },
    )
    _after_human_change(session, ctx, row, user)
    return row


def confirm_field(
    session: Session,
    ctx: Context,
    user: User,
    invoice_id: str,
    field: str,
    note: str,
    expected_version: int | None = None,
) -> InvoiceRow:
    """A person checked the page and the reading is right as is."""
    _require(user, AP_STAFF)
    row = _get(session, invoice_id)
    _require_state(row, State.NEEDS_ACTION)
    _check_version(row, expected_version)
    try:
        name = FieldName(field)
    except ValueError as e:
        raise InvalidValue(str(e)) from e
    if name.value not in row.human_verified:
        row.human_verified = [*row.human_verified, name.value]
    audit.append(
        session,
        row.id,
        user.name,
        "field_confirmed",
        {"field": name.value, "value": row.reading.get(name.value), "note": note},
    )
    _after_human_change(session, ctx, row, user)
    return row


def submit(
    session: Session,
    user: User,
    invoice_id: str,
    justification: str,
    vendor_verification: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> InvoiceRow:
    """Maker asks a checker to approve an invoice the engine did not approve."""
    _require(user, AP_STAFF)
    row = _get(session, invoice_id)
    _require_state(row, State.NEEDS_ACTION)
    _check_version(row, expected_version)
    if len(justification.strip()) < 10:
        raise InvalidValue("justification must explain why (at least 10 characters)")
    verification = None
    if row.decision == Status.HOLD:
        if vendor_verification is None:
            raise WorkflowError("a fraud hold can only be submitted with a vendor verification")
        try:
            verification = VendorVerification.model_validate(vendor_verification).model_dump()
        except ValidationError as e:
            raise InvalidValue(str(e)) from e
    if user.name not in row.makers:
        row.makers = [*row.makers, user.name]
    audit.append(
        session,
        row.id,
        user.name,
        "submitted",
        {
            "decision": row.decision,
            "justification": justification,
            "vendor_verification": verification,
        },
    )
    _set_state(session, row, State.AWAITING_APPROVAL, user.name)
    row.version += 1
    return row


def approve(
    session: Session,
    user: User,
    invoice_id: str,
    note: str = "",
    expected_version: int | None = None,
) -> InvoiceRow:
    _require(user, APPROVER)
    row = _get(session, invoice_id)
    _require_state(row, State.AWAITING_APPROVAL)
    _check_version(row, expected_version)
    if user.name in row.makers:
        raise Forbidden(
            f"maker-checker: {user.name!r} changed or submitted this invoice "
            "and cannot also approve it"
        )
    audit.append(session, row.id, user.name, "approved", {"note": note, "decision": row.decision})
    _set_state(session, row, State.APPROVED, user.name)
    row.version += 1
    return row


def reject(
    session: Session, user: User, invoice_id: str, reason: str, expected_version: int | None = None
) -> InvoiceRow:
    if user.role not in ROLES:
        raise Forbidden(f"unknown role {user.role!r}")
    row = _get(session, invoice_id)
    _require_state(row, State.NEEDS_ACTION, State.AWAITING_APPROVAL)
    _check_version(row, expected_version)
    if len(reason.strip()) < 5:
        raise InvalidValue("a rejection needs a reason")
    audit.append(session, row.id, user.name, "rejected", {"reason": reason})
    _set_state(session, row, State.REJECTED, user.name)
    row.version += 1
    return row


# ------------------------------------------------------------------ queries


def summary(session: Session) -> dict[str, Any]:
    rows = session.execute(
        select(
            InvoiceRow.state, InvoiceRow.decision, func.count(), func.sum(InvoiceRow.total)
        ).group_by(InvoiceRow.state, InvoiceRow.decision)
    ).all()
    return {
        "groups": [
            {"state": s, "decision": d, "count": n, "total": int(t or 0)} for s, d, n, t in rows
        ],
    }
