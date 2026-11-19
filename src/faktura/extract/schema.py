"""What the model must return. Kept flat and strict-schema friendly.

The model reports `uncertain_fields` instead of numeric confidences: self-reported
probabilities are poorly calibrated, while a flag can be checked directly against
errors in the eval (does flagging actually catch the mistakes?).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class FieldName(StrEnum):
    INVOICE_NUMBER = "invoice_number"
    TAX_INVOICE_NUMBER = "tax_invoice_number"
    INVOICE_DATE = "invoice_date"
    PO_NUMBER = "po_number"
    VENDOR_NAME = "vendor_name"
    VENDOR_NPWP = "vendor_npwp"
    BANK_NAME = "bank_name"
    BANK_ACCOUNT = "bank_account"
    LINES = "lines"
    DPP = "dpp"
    VAT = "vat"
    TOTAL = "total"


class ExtractedLine(BaseModel):
    description: str
    qty: int
    unit_price: int = Field(description="Rupiah, integer, no separators")
    amount: int = Field(description="Rupiah, integer, no separators")


class ExtractedInvoice(BaseModel):
    invoice_number: str | None
    tax_invoice_number: str | None
    invoice_date: str | None = Field(description="ISO format YYYY-MM-DD")
    po_number: str | None
    vendor_name: str | None
    vendor_npwp: str | None = Field(description="Digits only")
    bank_name: str | None
    bank_account: str | None = Field(description="Digits only")
    lines: list[ExtractedLine]
    dpp: int | None = Field(description="Tax base (DPP / Tax Base), rupiah integer")
    vat: int | None = Field(description="VAT (PPN), rupiah integer")
    total: int | None = Field(description="Grand total, rupiah integer")
    uncertain_fields: list[FieldName] = Field(
        description="Fields that are hard to read, ambiguous, or possibly misread"
    )


EXTRACTION_PROMPT = """\
You read Indonesian supplier invoices (faktur). Extract the fields exactly as printed.
Rules:
- Copy values as printed. Do not correct, recompute, or infer values that are not on the page.
  If the printed total looks wrong, still return the printed total.
- Money: integer rupiah. "Rp 1.234.567" -> 1234567.
- NPWP and bank account: digits only, no spaces or punctuation.
- Date: convert to YYYY-MM-DD.
- Use null for a field that is not on the page.
- List every line item in printed order.
- Put a field in uncertain_fields if any character in it is hard to read."""
