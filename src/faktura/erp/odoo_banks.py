"""Write an approved bank-account change to Odoo, safely re-runnable.

Faktura verifies the change (request, call-back to the number on file, approval by a
third person; see app/bank_changes.py). Odoo stays the system of record for vendor
master data, so the verified account is written there too:

    1. find the vendor's bank account with the new number, else create it
    2. trust it (allow_out_payment), which needs the "Validate bank account" right
    3. stop trusting the vendor's other accounts, so the old one can no longer be paid

Each step checks its own result first, so re-running after a crash or a lost response
does the remaining steps only.
"""

from __future__ import annotations

from typing import Any

from faktura.erp.odoo import OdooClient
from faktura.erp.odoo_bills import NeedsAttention, _digits, _one


def apply_bank_change(client: OdooClient, change: dict[str, Any]) -> int:
    """Make `change["new"]` the vendor's only trusted account. Returns its Odoo id."""
    partner = _one(client, "res.partner", [("ref", "=", change["vendor_id"])], ["name"])
    if partner is None:
        raise NeedsAttention(f"vendor {change['vendor_id']} not found in Odoo")
    new = change["new"]
    banks = client.search_read(
        "res.partner.bank",
        [("partner_id", "=", partner["id"])],
        ["account_number", "allow_out_payment"],
    )
    target = next((b for b in banks if _digits(b["account_number"]) == new["bank_account"]), None)
    if target is None:
        [bank_id] = client.call(
            "res.partner.bank",
            "create",
            vals_list=[
                {
                    "partner_id": partner["id"],
                    "account_number": new["bank_account"],
                    "bank_name": new["bank_name"],
                }
            ],
        )
        target = {"id": bank_id, "allow_out_payment": False}
    if not target["allow_out_payment"]:
        client.call(
            "res.partner.bank", "write", ids=[target["id"]], vals={"allow_out_payment": True}
        )
    others = [b["id"] for b in banks if b["id"] != target["id"] and b["allow_out_payment"]]
    if others:
        client.call("res.partner.bank", "write", ids=others, vals={"allow_out_payment": False})
    return target["id"]
