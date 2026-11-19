"""Core document schemas. All money is integer rupiah; no floats anywhere."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field

BPS_DENOMINATOR = 10_000


def compute_vat(dpp: int, vat_bps: int) -> int:
    """VAT on the tax base (DPP), floored to whole rupiah. `vat_bps` is basis points."""
    return dpp * vat_bps // BPS_DENOMINATOR


class Vendor(BaseModel):
    vendor_id: str
    name: str
    npwp: str = Field(pattern=r"^\d{16}$")
    address: str
    bank_name: str
    bank_account: str


class POLine(BaseModel):
    sku: str
    description: str
    qty: int = Field(gt=0)
    unit_price: int = Field(gt=0)


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    date: dt.date
    lines: list[POLine]


class GRLine(BaseModel):
    sku: str
    qty_received: int = Field(ge=0)


class GoodsReceipt(BaseModel):
    gr_number: str
    po_number: str
    date: dt.date
    lines: list[GRLine]


class InvoiceLine(BaseModel):
    sku: str
    description: str
    qty: int = Field(gt=0)
    unit_price: int = Field(gt=0)
    amount: int


class Invoice(BaseModel):
    """What is printed on the invoice. This is also the extraction target."""

    invoice_id: str  # internal id of the file, not printed
    invoice_number: str
    tax_invoice_number: str
    invoice_date: dt.date
    po_number: str
    vendor_name: str
    vendor_npwp: str
    bank_name: str
    bank_account: str
    lines: list[InvoiceLine]
    dpp: int
    vat: int
    total: int


class AnomalyType(StrEnum):
    PRICE_MISMATCH = "price_mismatch"
    QTY_OVER_RECEIVED = "qty_over_received"
    DUPLICATE_INVOICE = "duplicate_invoice"
    NPWP_MISMATCH = "npwp_mismatch"
    VAT_MISCALC = "vat_miscalc"
    BANK_ACCOUNT_CHANGED = "bank_account_changed"


class InvoiceLabel(BaseModel):
    """Ground truth for one synthetic invoice."""

    invoice_id: str
    invoice: Invoice
    anomalies: list[AnomalyType]
    duplicate_of: str | None = None
    note: str = ""
    # How the PDF was rendered (layout, language, scan effects), for error analysis.
    render: dict[str, str | int | float | bool] = Field(default_factory=dict)
