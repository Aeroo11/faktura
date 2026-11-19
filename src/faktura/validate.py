"""Deterministic validators that run on every extraction before matching.

They answer one question per field: can this value be trusted as read? Issues come in
three kinds, because the next step treats them differently:

- FORMAT        the value cannot be valid at all (wrong length, unparseable date).
                Almost certainly a misread: re-read or send to a human, never to fraud.
- INCONSISTENT  the document's own arithmetic does not add up. Printed invoices are
                internally consistent, so this also points to a misread.
- MASTER        the value differs from master data or the PO. Either a misread or a
                genuine business exception (price change, new bank account). The matching
                stage decides which; this module only refuses to trust it silently.

None of these rules call a model, so every flag can be explained to an auditor.
"""

from __future__ import annotations

import datetime as dt
import re
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel

from faktura.extract.schema import ExtractedInvoice, FieldName
from faktura.master import MasterData
from faktura.schemas import PurchaseOrder, Vendor, compute_vat

NPWP_LEN = 16
TAX_INVOICE_LEN = 17  # synthetic format; see docs/assumptions.md
NAME_SIMILARITY_MIN = 0.9
LINE_MATCH_MIN = 0.8


class IssueKind(StrEnum):
    FORMAT = "format"
    INCONSISTENT = "inconsistent"
    MASTER = "master"


class Issue(BaseModel):
    code: str
    kind: IssueKind
    fields: list[FieldName]
    message: str


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def number_shape(s: str) -> str:
    """'INV/V007/2025/0004' -> 'AAA/A999/9999/9999'."""
    return re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", s.strip()))


def resolve_vendor(ext: ExtractedInvoice, md: MasterData) -> tuple[Vendor | None, str]:
    """Identify the vendor: via the PO if it exists, else by closest name."""
    po = md.purchase_orders.get((ext.po_number or "").strip())
    if po is not None:
        return md.vendors[po.vendor_id], "po"
    best, score = None, 0.0
    for v in md.vendors.values():
        r = SequenceMatcher(None, _norm(ext.vendor_name), _norm(v.name)).ratio()
        if r > score:
            best, score = v, r
    return (best, "name") if score >= NAME_SIMILARITY_MIN else (None, "none")


def check_format(ext: ExtractedInvoice) -> list[Issue]:
    issues: list[Issue] = []
    if ext.vendor_npwp is not None and len(_digits(ext.vendor_npwp)) != NPWP_LEN:
        issues.append(
            Issue(
                code="npwp_length",
                kind=IssueKind.FORMAT,
                fields=[FieldName.VENDOR_NPWP],
                message=f"NPWP has {len(_digits(ext.vendor_npwp))} digits, expected {NPWP_LEN}",
            )
        )
    if ext.tax_invoice_number is not None and len(_digits(ext.tax_invoice_number)) != (
        TAX_INVOICE_LEN
    ):
        issues.append(
            Issue(
                code="tax_invoice_length",
                kind=IssueKind.FORMAT,
                fields=[FieldName.TAX_INVOICE_NUMBER],
                message="tax invoice number has the wrong length",
            )
        )
    for name, value in [
        (FieldName.VENDOR_NPWP, ext.vendor_npwp),
        (FieldName.TAX_INVOICE_NUMBER, ext.tax_invoice_number),
        (FieldName.INVOICE_NUMBER, ext.invoice_number),
        (FieldName.INVOICE_DATE, ext.invoice_date),
        (FieldName.TOTAL, ext.total),
        # DPP and VAT too: an unreadable VAT (e.g. under a stamp) must reach a human,
        # otherwise a wrong VAT on that invoice can never be caught.
        (FieldName.DPP, ext.dpp),
        (FieldName.VAT, ext.vat),
        (FieldName.BANK_ACCOUNT, ext.bank_account),
        (FieldName.PO_NUMBER, ext.po_number),
    ]:
        if value is None:
            issues.append(
                Issue(
                    code="missing",
                    kind=IssueKind.FORMAT,
                    fields=[name],
                    message=f"{name.value} not found on the page",
                )
            )
    if ext.invoice_date is not None:
        try:
            dt.date.fromisoformat(ext.invoice_date)
        except ValueError:
            issues.append(
                Issue(
                    code="date_unparseable",
                    kind=IssueKind.FORMAT,
                    fields=[FieldName.INVOICE_DATE],
                    message=f"unparseable date {ext.invoice_date!r}",
                )
            )
    if not ext.lines:
        issues.append(
            Issue(
                code="no_lines",
                kind=IssueKind.FORMAT,
                fields=[FieldName.LINES],
                message="no line items",
            )
        )
    return issues


def check_arithmetic(ext: ExtractedInvoice) -> list[Issue]:
    issues: list[Issue] = []
    bad = [i for i, ln in enumerate(ext.lines, 1) if ln.qty * ln.unit_price != ln.amount]
    if bad:
        issues.append(
            Issue(
                code="line_amount",
                kind=IssueKind.INCONSISTENT,
                fields=[FieldName.LINES],
                message=f"qty x price != amount on lines {bad}",
            )
        )
    if ext.dpp is not None and ext.lines and sum(ln.amount for ln in ext.lines) != ext.dpp:
        issues.append(
            Issue(
                code="dpp_sum",
                kind=IssueKind.INCONSISTENT,
                fields=[FieldName.DPP, FieldName.LINES],
                message="line amounts do not sum to DPP",
            )
        )
    if None not in (ext.dpp, ext.vat, ext.total) and ext.dpp + ext.vat != ext.total:
        issues.append(
            Issue(
                code="total_sum",
                kind=IssueKind.INCONSISTENT,
                fields=[FieldName.DPP, FieldName.VAT, FieldName.TOTAL],
                message="DPP + VAT != total",
            )
        )
    return issues


def match_po_line(description: str, po: PurchaseOrder):
    best, score = None, 0.0
    for pl in po.lines:
        r = SequenceMatcher(None, _norm(description), _norm(pl.description)).ratio()
        if r > score:
            best, score = pl, r
    return best if score >= LINE_MATCH_MIN else None


def check_master(ext: ExtractedInvoice, md: MasterData, vat_bps: int) -> list[Issue]:
    issues: list[Issue] = []
    vendor, _ = resolve_vendor(ext, md)
    po = md.purchase_orders.get((ext.po_number or "").strip())

    if ext.po_number is not None and po is None:
        issues.append(
            Issue(
                code="po_unknown",
                kind=IssueKind.MASTER,
                fields=[FieldName.PO_NUMBER],
                message=f"PO {ext.po_number!r} not found",
            )
        )
    if vendor is None:
        issues.append(
            Issue(
                code="vendor_unknown",
                kind=IssueKind.MASTER,
                fields=[FieldName.VENDOR_NAME, FieldName.PO_NUMBER],
                message="vendor could not be identified",
            )
        )
        return issues

    if SequenceMatcher(None, _norm(ext.vendor_name), _norm(vendor.name)).ratio() < (
        NAME_SIMILARITY_MIN
    ):
        issues.append(
            Issue(
                code="vendor_name",
                kind=IssueKind.MASTER,
                fields=[FieldName.VENDOR_NAME],
                message="vendor name differs from master",
            )
        )
    if ext.vendor_npwp is not None and _digits(ext.vendor_npwp) != vendor.npwp:
        issues.append(
            Issue(
                code="npwp_master",
                kind=IssueKind.MASTER,
                fields=[FieldName.VENDOR_NPWP],
                message="NPWP differs from vendor master",
            )
        )
    if ext.bank_account is not None and _digits(ext.bank_account) != vendor.bank_account:
        issues.append(
            Issue(
                code="bank_master",
                kind=IssueKind.MASTER,
                fields=[FieldName.BANK_ACCOUNT],
                message="bank account differs from vendor master",
            )
        )
    if _norm(ext.bank_name) != _norm(vendor.bank_name):
        issues.append(
            Issue(
                code="bank_name_master",
                kind=IssueKind.MASTER,
                fields=[FieldName.BANK_NAME],
                message="bank differs from vendor master",
            )
        )

    shapes = {number_shape(n) for n in md.approved_invoice_numbers.get(vendor.vendor_id, [])}
    if ext.invoice_number and shapes and number_shape(ext.invoice_number) not in shapes:
        issues.append(
            Issue(
                code="invoice_number_shape",
                kind=IssueKind.MASTER,
                fields=[FieldName.INVOICE_NUMBER],
                message=f"{ext.invoice_number!r} does not match this vendor's "
                f"numbering {sorted(shapes)}",
            )
        )

    if ext.dpp is not None and ext.vat is not None and ext.vat != compute_vat(ext.dpp, vat_bps):
        issues.append(
            Issue(
                code="vat_rate",
                kind=IssueKind.MASTER,
                fields=[FieldName.VAT],
                message="VAT does not match the configured rate",
            )
        )

    if po is not None:
        if ext.invoice_date and _parse(ext.invoice_date) and _parse(ext.invoice_date) < po.date:
            issues.append(
                Issue(
                    code="date_before_po",
                    kind=IssueKind.MASTER,
                    fields=[FieldName.INVOICE_DATE],
                    message="invoice dated before its PO",
                )
            )
        gr = md.goods_receipts.get(po.po_number)
        received = {g.sku: g.qty_received for g in gr.lines} if gr else {}
        unmatched, price_diff, over_received = [], [], []
        for ln in ext.lines:
            pl = match_po_line(ln.description, po)
            if pl is None:
                unmatched.append(ln.description)
                continue
            if ln.unit_price != pl.unit_price:
                price_diff.append(pl.sku)
            if ln.qty > received.get(pl.sku, 0):
                over_received.append(pl.sku)
        if unmatched or len(ext.lines) > len(po.lines):
            issues.append(
                Issue(
                    code="lines_vs_po",
                    kind=IssueKind.MASTER,
                    fields=[FieldName.LINES],
                    message=f"lines not on the PO: {unmatched}",
                )
            )
        if price_diff:
            issues.append(
                Issue(
                    code="price_vs_po",
                    kind=IssueKind.MASTER,
                    fields=[FieldName.LINES],
                    message=f"unit price differs from PO: {price_diff}",
                )
            )
        if over_received:
            issues.append(
                Issue(
                    code="qty_vs_receipt",
                    kind=IssueKind.MASTER,
                    fields=[FieldName.LINES],
                    message=f"billed more than received: {over_received}",
                )
            )
    return issues


def _parse(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def validate(ext: ExtractedInvoice, md: MasterData, vat_bps: int) -> list[Issue]:
    return check_format(ext) + check_arithmetic(ext) + check_master(ext, md, vat_bps)


def suspect_fields(issues: list[Issue]) -> set[FieldName]:
    return {f for i in issues for f in i.fields}
