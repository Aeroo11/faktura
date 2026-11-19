"""Field-level comparison of an extraction against the printed ground truth."""

from __future__ import annotations

import datetime as dt
import re

from faktura.extract.schema import ExtractedInvoice, FieldName
from faktura.schemas import Invoice

_DIGITS = re.compile(r"\D")
_SPACE = re.compile(r"\s+")


def norm_text(s: str | None) -> str | None:
    if s is None:
        return None
    return _SPACE.sub(" ", s).strip().casefold()


def norm_digits(s: str | None) -> str | None:
    return None if s is None else _DIGITS.sub("", s)


def parse_date(s: str | None) -> dt.date | None:
    if s is None:
        return None
    try:
        return dt.date.fromisoformat(s.strip())
    except ValueError:
        return None


def lines_correct(ext: ExtractedInvoice, truth: Invoice) -> bool:
    if len(ext.lines) != len(truth.lines):
        return False
    return all(
        (e.qty, e.unit_price, e.amount) == (t.qty, t.unit_price, t.amount)
        and norm_text(e.description) == norm_text(t.description)
        for e, t in zip(ext.lines, truth.lines, strict=True)
    )


def score_fields(ext: ExtractedInvoice, truth: Invoice) -> dict[FieldName, bool]:
    f = FieldName
    return {
        f.INVOICE_NUMBER: norm_text(ext.invoice_number) == norm_text(truth.invoice_number),
        f.TAX_INVOICE_NUMBER: norm_digits(ext.tax_invoice_number) == truth.tax_invoice_number,
        f.INVOICE_DATE: parse_date(ext.invoice_date) == truth.invoice_date,
        f.PO_NUMBER: norm_text(ext.po_number) == norm_text(truth.po_number),
        f.VENDOR_NAME: norm_text(ext.vendor_name) == norm_text(truth.vendor_name),
        f.VENDOR_NPWP: norm_digits(ext.vendor_npwp) == truth.vendor_npwp,
        f.BANK_NAME: norm_text(ext.bank_name) == norm_text(truth.bank_name),
        f.BANK_ACCOUNT: norm_digits(ext.bank_account) == truth.bank_account,
        f.LINES: lines_correct(ext, truth),
        f.DPP: ext.dpp == truth.dpp,
        f.VAT: ext.vat == truth.vat,
        f.TOTAL: ext.total == truth.total,
    }


def all_wrong() -> dict[FieldName, bool]:
    """Score for a failed call: count it as wrong rather than dropping it."""
    return dict.fromkeys(FieldName, False)
