"""Three-way match: turn an extraction plus validator issues into a payment decision.

Decisions, from least to most severe (an invoice gets the most severe one that applies):

    approve              everything agrees with PO, goods receipt, and vendor master
    review_reading       the reading itself is in doubt; a human checks it against the page
    business_exception   the reading is fine but the invoice deviates from PO or receipt
                         (price, quantity, VAT); needs business approval
    fraud_hold           payment is held: changed bank account, NPWP mismatch, duplicate

Policy notes:
- A bank account or NPWP that differs from the vendor master is always held, even though
  it might be a misread. Changed payment details are the most common AP fraud pattern
  (business email compromise); the safe process is to verify by calling a known vendor
  contact, never to pay automatically.
- A learned misread model (learn/misread.py) may triage such a difference as
  `probable_misread` (review_reading) when it looks exactly like one of the reader's known
  misreads. This only changes who looks first: review_reading is never auto-approved, and
  once a person has checked the field against the page (confirmed or corrected it), the
  master comparison applies unchanged, so a value really printed differently is held.
- Unknown issue codes fail safe to review_reading.
- Every decision carries its reasons, so it can be explained to an auditor.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel

from faktura.extract.schema import ExtractedInvoice
from faktura.learn.misread import MisreadModel
from faktura.master import MasterData
from faktura.schemas import Vendor
from faktura.validate import Issue, match_po_line, resolve_vendor, validate


class Status(StrEnum):
    APPROVE = "approve"
    REVIEW = "review_reading"
    EXCEPTION = "business_exception"
    HOLD = "fraud_hold"


SEVERITY = {Status.APPROVE: 0, Status.REVIEW: 1, Status.EXCEPTION: 2, Status.HOLD: 3}

ISSUE_POLICY: dict[str, Status] = {
    # The reading cannot be trusted as is.
    "npwp_length": Status.REVIEW,
    "tax_invoice_length": Status.REVIEW,
    "missing": Status.REVIEW,
    "date_unparseable": Status.REVIEW,
    "no_lines": Status.REVIEW,
    "line_amount": Status.REVIEW,
    "dpp_sum": Status.REVIEW,
    "total_sum": Status.REVIEW,
    "po_unknown": Status.REVIEW,
    "vendor_unknown": Status.REVIEW,
    "vendor_name": Status.REVIEW,
    "invoice_number_shape": Status.REVIEW,
    "date_before_po": Status.REVIEW,
    "lines_vs_po": Status.REVIEW,
    # The invoice deviates from what was ordered or received.
    "price_vs_po": Status.EXCEPTION,
    "qty_vs_receipt": Status.EXCEPTION,
    "vat_rate": Status.EXCEPTION,
    # Payment details or identity differ from the vendor master.
    "npwp_master": Status.HOLD,
    "bank_master": Status.HOLD,
    "bank_name_master": Status.HOLD,
}


class Reason(BaseModel):
    code: str
    status: Status
    message: str
    fields: list[str] = []  # fields this reason is about, for highlighting in the UI


class Decision(BaseModel):
    invoice_id: str
    status: Status
    reasons: list[Reason]
    vendor_id: str | None = None


def _norm_id(s: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


def _line_signature(ext: ExtractedInvoice) -> tuple[tuple[int, int], ...]:
    """Order-independent (qty, unit price) pairs: the amounts, not the wording."""
    return tuple(sorted((ln.qty, ln.unit_price) for ln in ext.lines))


@dataclass
class Ledger:
    """What AP has already received: for duplicate detection and cumulative billing."""

    seen: list[dict] = field(default_factory=list)
    billed: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))

    def find_duplicate(self, vendor_id: str | None, ext: ExtractedInvoice) -> Reason | None:
        num, tax = _norm_id(ext.invoice_number), _norm_id(ext.tax_invoice_number)
        name, npwp = _norm_id(ext.vendor_name), re.sub(r"\D", "", ext.vendor_npwp or "")
        lines = _line_signature(ext)
        for prev in self.seen:
            same_vendor = bool(
                (vendor_id and prev["vendor_id"] == vendor_id)
                or (name and prev["name"] == name)
                or (npwp and prev["npwp"] == npwp)
            )
            # Money is read far more reliably than identifiers, so match on amounts too.
            # Found when an original photo had invoice, tax, and PO numbers all misread,
            # and its correctly read resubmission matched nothing in the ledger.
            if (
                same_vendor
                and ext.total is not None
                and prev["total"] == ext.total
                and lines
                and prev["lines"] == lines
            ):
                msg = f"same vendor, total, and line amounts as {prev['id']}"
                return Reason(code="duplicate_amounts", status=Status.HOLD, message=msg)
            if vendor_id and prev["vendor_id"] == vendor_id and num and prev["num"] == num:
                msg = f"same vendor and invoice number as {prev['id']}"
                return Reason(code="duplicate_number", status=Status.HOLD, message=msg)
            if tax and prev["tax"] == tax:
                return Reason(
                    code="duplicate_tax_invoice",
                    status=Status.HOLD,
                    message=f"same tax invoice number as {prev['id']}",
                )
            if (
                vendor_id
                and prev["vendor_id"] == vendor_id
                and ext.po_number
                and prev["po"] == _norm_id(ext.po_number)
                and ext.total is not None
                and prev["total"] == ext.total
            ):
                # Catches a resubmission whose numbers were read differently.
                return Reason(
                    code="duplicate_po_total",
                    status=Status.HOLD,
                    message=f"same vendor, PO, and total as {prev['id']}",
                )
        return None

    def record(self, invoice_id: str, vendor_id: str | None, ext: ExtractedInvoice) -> None:
        self.seen.append(
            {
                "id": invoice_id,
                "vendor_id": vendor_id,
                "num": _norm_id(ext.invoice_number),
                "tax": _norm_id(ext.tax_invoice_number),
                "po": _norm_id(ext.po_number),
                "total": ext.total,
                "name": _norm_id(ext.vendor_name),
                "npwp": re.sub(r"\D", "", ext.vendor_npwp or ""),
                "lines": _line_signature(ext),
            }
        )


def cumulative_overbilling(ext: ExtractedInvoice, md: MasterData, ledger: Ledger) -> Reason | None:
    """Earlier invoices plus this one bill more than was received on some PO line."""
    po = md.purchase_orders.get((ext.po_number or "").strip())
    gr = md.goods_receipts.get(po.po_number) if po else None
    if po is None or gr is None:
        return None
    received = {g.sku: g.qty_received for g in gr.lines}
    over = []
    for ln in ext.lines:
        pl = match_po_line(ln.description, po)
        if pl is None:
            continue
        before = ledger.billed[(po.po_number, pl.sku)]
        if before and before + ln.qty > received.get(pl.sku, 0):
            over.append(pl.sku)
    if not over:
        return None
    return Reason(
        code="qty_cumulative",
        status=Status.EXCEPTION,
        fields=["lines"],
        message=f"earlier invoices plus this one exceed receipt: {over}",
    )


def add_billing(ext: ExtractedInvoice, md: MasterData, ledger: Ledger) -> None:
    po = md.purchase_orders.get((ext.po_number or "").strip())
    if po is None:
        return
    for ln in ext.lines:
        pl = match_po_line(ln.description, po)
        if pl is not None:
            ledger.billed[(po.po_number, pl.sku)] += ln.qty


def decide(
    invoice_id: str,
    ext: ExtractedInvoice,
    md: MasterData,
    ledger: Ledger,
    vat_bps: int,
    issues: list[Issue] | None = None,
    trust_model_flags: bool = True,
    misread: MisreadModel | None = None,
    verified: frozenset[str] | set[str] = frozenset(),
) -> Decision:
    """Decide one invoice and update the ledger. Invoices must be fed in arrival order.

    `verified`: fields a person has checked against the page; never triaged as misreads.
    """
    if issues is None:
        issues = validate(ext, md, vat_bps)
    vendor, _ = resolve_vendor(ext, md)
    vendor_id = vendor.vendor_id if vendor else None

    reasons = [
        Reason(
            code=i.code,
            status=ISSUE_POLICY.get(i.code, Status.REVIEW),
            message=i.message,
            fields=[f.value for f in i.fields],
        )
        for i in issues
    ]
    if misread is not None and vendor is not None:
        reasons = [_triage_misread(r, ext, vendor, misread, verified) for r in reasons]
    if trust_model_flags and ext.uncertain_fields:
        names = sorted(f.value for f in ext.uncertain_fields)
        reasons.append(
            Reason(
                code="model_uncertain",
                status=Status.REVIEW,
                fields=names,
                message=f"model marked as hard to read: {', '.join(names)}",
            )
        )

    dup = ledger.find_duplicate(vendor_id, ext)
    if dup is not None:
        reasons.append(dup)
    else:
        over = cumulative_overbilling(ext, md, ledger)
        if over is not None:
            reasons.append(over)
        add_billing(ext, md, ledger)  # duplicates never count towards billed quantity
    ledger.record(invoice_id, vendor_id, ext)

    status = max((r.status for r in reasons), key=SEVERITY.__getitem__, default=Status.APPROVE)
    return Decision(invoice_id=invoice_id, status=status, reasons=reasons, vendor_id=vendor_id)


_MASTER_VALUE = {  # issue code -> (reading field, vendor-master attribute)
    "npwp_master": ("vendor_npwp", "npwp"),
    "bank_master": ("bank_account", "bank_account"),
}
_LABEL = {"vendor_npwp": "NPWP", "bank_account": "Bank account"}


def _triage_misread(
    r: Reason,
    ext: ExtractedInvoice,
    vendor: Vendor,
    misread: MisreadModel,
    verified: frozenset[str] | set[str],
) -> Reason:
    """Turn a master mismatch that looks like a known misread into a check of the page.

    The message names neither the master value nor the edits: a person asked to "fix" a
    digit towards a value they were shown could make a fraudulent invoice look clean.
    """
    if r.code not in _MASTER_VALUE:
        return r
    field_name, master_attr = _MASTER_VALUE[r.code]
    if field_name in verified:
        return r
    if misread.explain(getattr(ext, field_name), getattr(vendor, master_attr)) is None:
        return r
    return Reason(
        code="probable_misread",
        status=Status.REVIEW,
        fields=[field_name],
        message=(
            f"{_LABEL[field_name]} differs from the vendor master in a way this reader often "
            "misreads; check every digit against the page. If the page shows it as read, "
            "confirm it and the invoice is held for vendor verification."
        ),
    )
