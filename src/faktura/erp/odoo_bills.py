"""Post an approved invoice to Odoo as a vendor bill, safely re-runnable.

JSON-2 gives one transaction per call, so posting is a sequence of steps, each of
which first checks whether it already happened:

    1. find the bill by its Faktura id (custom field x_faktura_id), else create it as a
       draft, linked line by line to the purchase order
    2. make the bill's amounts equal the vendor's invoice (rounding only, < Rp 1)
    3. attach the invoice PDF
    4. post it

If the worker dies or a response is lost after any step, the next run finds the draft
or posted bill and continues; it never creates a second bill. Anything that needs a
person (unknown vendor or PO, untrusted bank account, amounts that disagree) raises
NeedsAttention and leaves the bill as a draft.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faktura.erp.odoo import OdooClient
from faktura.extract.schema import ExtractedInvoice
from faktura.schemas import POLine, PurchaseOrder
from faktura.validate import match_po_line

MOVE_FIELDS = ["name", "state", "amount_untaxed", "amount_tax", "amount_total", "tax_totals"]


class NeedsAttention(Exception):
    """The ERP cannot take this bill as is; retrying will not help."""


@dataclass(frozen=True)
class PostedBill:
    move_id: int
    name: str
    created: bool  # False when an earlier run had already created it


def _digits(s: str | None) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _one(client: OdooClient, model: str, domain: list, fields: list[str]) -> dict | None:
    rows = client.search_read(model, domain, fields)
    if len(rows) > 1:
        raise NeedsAttention(f"{len(rows)} {model} records match {domain}")
    return rows[0] if rows else None


def find_bill(client: OdooClient, invoice_id: str) -> dict | None:
    return _one(client, "account.move", [("x_faktura_id", "=", invoice_id)], MOVE_FIELDS)


def _bill_lines(client: OdooClient, ext: ExtractedInvoice) -> list[dict[str, Any]]:
    """One bill line per invoice line, each linked to the PO line it bills."""
    po = _one(client, "purchase.order", [("name", "=", ext.po_number)], ["name"])
    if po is None:
        raise NeedsAttention(f"PO {ext.po_number} not found in Odoo")
    po_lines = client.search_read(
        "purchase.order.line",
        [("order_id", "=", po["id"]), ("display_type", "=", False)],
        ["name", "product_id", "product_qty", "price_unit", "tax_ids"],
    )
    by_id = {ln["id"]: ln for ln in po_lines}
    # Reuse the engine's line matcher so the bill links lines the way they were checked.
    shape = PurchaseOrder(
        po_number=po["name"],
        vendor_id="-",
        date="2000-01-01",
        lines=[
            POLine(sku=str(ln["id"]), description=ln["name"], qty=1, unit_price=1)
            for ln in po_lines
        ],
    )
    out = []
    for line in ext.lines:
        match = match_po_line(line.description, shape)
        if match is None:
            raise NeedsAttention(f"invoice line {line.description!r} matches no line of the PO")
        pl = by_id[int(match.sku)]
        out.append(
            {
                "purchase_line_id": pl["id"],
                "product_id": pl["product_id"][0],
                "name": line.description,
                "quantity": line.qty,
                "price_unit": line.unit_price,
                "tax_ids": [[6, 0, pl["tax_ids"]]],
            }
        )
    return out


def _trusted_bank(client: OdooClient, partner_id: int, account: str | None) -> int:
    """The vendor's trusted Odoo bank account that the invoice asks to be paid into."""
    banks = client.search_read(
        "res.partner.bank",
        [("partner_id", "=", partner_id), ("allow_out_payment", "=", True)],
        ["account_number"],
    )
    for b in banks:
        if _digits(b["account_number"]) == _digits(account):
            return b["id"]
    raise NeedsAttention(
        "the invoice's bank account is not a trusted account of this vendor in Odoo; "
        "verify it with the vendor and trust it in Odoo first"
    )


def create_draft(client: OdooClient, invoice_id: str, payload: dict[str, Any]) -> int:
    ext = ExtractedInvoice.model_validate(payload["reading"])
    partner = _one(client, "res.partner", [("ref", "=", payload["vendor_id"])], ["name"])
    if partner is None:
        raise NeedsAttention(f"vendor {payload['vendor_id']} not found in Odoo")
    vals = {
        "move_type": "in_invoice",
        "partner_id": partner["id"],
        "partner_bank_id": _trusted_bank(client, partner["id"], ext.bank_account),
        "ref": ext.invoice_number,
        "invoice_date": ext.invoice_date,
        "invoice_origin": ext.po_number,
        "x_faktura_id": invoice_id,
        "invoice_line_ids": [[0, 0, ln] for ln in _bill_lines(client, ext)],
    }
    return client.call("account.move", "create", vals_list=[vals])[0]


def match_invoice_amounts(client: OdooClient, move: dict, ext: ExtractedInvoice) -> dict:
    """Make the bill equal the vendor's invoice, or refuse.

    Odoo computes VAT to the sen (IDR has 2 decimals); the invoice prints whole rupiah.
    That rounding gap (< Rp 1) is closed the way an AP clerk would in the UI: by
    setting the tax amount to the one on the vendor's tax invoice. Any larger gap means
    the bill and the approved invoice disagree, and a person has to look.
    """
    if round(move["amount_untaxed"], 2) != ext.dpp:
        raise NeedsAttention(f"bill DPP {move['amount_untaxed']} != invoice DPP {ext.dpp}")
    gap = ext.vat - move["amount_tax"]
    if gap and abs(gap) < 1:
        totals = move["tax_totals"]
        groups = [g for st in totals["subtotals"] for g in st["tax_groups"]]
        if len(groups) != 1:
            raise NeedsAttention(f"expected one tax group, found {len(groups)}")
        groups[0]["tax_amount_currency"] = ext.vat
        client.call("account.move", "write", ids=[move["id"]], vals={"tax_totals": totals})
        move = client.call("account.move", "read", ids=[move["id"]], fields=MOVE_FIELDS)[0]
    if (round(move["amount_tax"], 2), round(move["amount_total"], 2)) != (ext.vat, ext.total):
        raise NeedsAttention(
            f"bill VAT/total {move['amount_tax']}/{move['amount_total']} != "
            f"invoice {ext.vat}/{ext.total}"
        )
    return move


def attach_pdf(client: OdooClient, move_id: int, invoice_id: str, pdf_path: str) -> None:
    name = f"{invoice_id}.pdf"
    domain = [("res_model", "=", "account.move"), ("res_id", "=", move_id), ("name", "=", name)]
    if client.search_read("ir.attachment", domain, ["id"]):
        return
    content = Path(pdf_path).read_bytes()
    # Odoo 20: the content field is `raw` (base64 over JSON); `datas` no longer exists and
    # the mimetype is detected from the content.
    [att_id] = client.call(
        "ir.attachment",
        "create",
        vals_list=[
            {
                "name": name,
                "res_model": "account.move",
                "res_id": move_id,
                "raw": base64.b64encode(content).decode(),
            }
        ],
    )
    [att] = client.call("ir.attachment", "read", ids=[att_id], fields=["file_size", "mimetype"])
    if att["file_size"] != len(content) or att["mimetype"] != "application/pdf":
        raise NeedsAttention(f"attachment stored as {att}, expected {len(content)} bytes of PDF")


def post_bill(client: OdooClient, invoice_id: str, payload: dict[str, Any]) -> PostedBill:
    ext = ExtractedInvoice.model_validate(payload["reading"])
    move = find_bill(client, invoice_id)
    created = move is None
    if move is None:
        move_id = create_draft(client, invoice_id, payload)
        move = client.call("account.move", "read", ids=[move_id], fields=MOVE_FIELDS)[0]
    if move["state"] == "cancel":
        raise NeedsAttention(f"bill {move['name']} was cancelled in Odoo")
    if move["state"] == "draft":
        move = match_invoice_amounts(client, move, ext)
        attach_pdf(client, move["id"], invoice_id, payload["pdf_path"])
        client.call("account.move", "action_post", ids=[move["id"]])
        move = client.call("account.move", "read", ids=[move["id"]], fields=["name", "state"])[0]
    return PostedBill(move_id=move["id"], name=move["name"], created=created)
