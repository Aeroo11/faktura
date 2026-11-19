"""Read vendors, purchase orders, and goods receipts from Odoo into Faktura's models.

Field mapping (Odoo 20, see docs/erp.md):

    Vendor.vendor_id     res.partner.ref
    Vendor.npwp          res.partner.vat
    Vendor.bank_*        res.partner.bank (trusted only: allow_out_payment)
    PurchaseOrder        purchase.order (confirmed) + purchase.order.line
    POLine.sku           product.product.default_code
    GoodsReceipt         done incoming stock.picking; quantities from
                         purchase.order.line.qty_received, which Odoo keeps net of returns

Only trusted bank accounts count as master data. A new account added in Odoo is
untrusted until someone verifies it, so an invoice paying into it is held.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from faktura.erp.odoo import OdooClient
from faktura.master import MasterData
from faktura.schemas import GoodsReceipt, GRLine, POLine, PurchaseOrder, Vendor


def _m2o_id(value: Any) -> int | None:
    """Many2one fields come back as [id, display_name], or False when empty."""
    return value[0] if value else None


def _whole(x: float, what: str) -> int:
    if x != int(x):
        raise ValueError(f"{what} is not a whole number: {x}")
    return int(x)


def _date(value: str) -> dt.date:
    # Odoo datetimes are UTC strings "YYYY-MM-DD HH:MM:SS".
    return dt.date.fromisoformat(value[:10])


def fetch_vendors(client: OdooClient) -> tuple[dict[str, Vendor], dict[int, str]]:
    """Vendors keyed by vendor id, and the Odoo partner id -> vendor id map."""
    partners = client.search_read(
        "res.partner",
        [("ref", "!=", False), ("supplier_rank", ">", 0)],
        ["ref", "name", "vat", "street"],
    )
    banks = client.search_read(
        "res.partner.bank",
        [("partner_id", "in", [p["id"] for p in partners]), ("allow_out_payment", "=", True)],
        ["partner_id", "account_number", "bank_name"],
        order="sequence, id",
    )
    trusted: dict[int, dict] = {}
    for b in banks:
        trusted.setdefault(_m2o_id(b["partner_id"]), b)  # first by sequence wins
    vendors = {}
    for p in partners:
        bank = trusted.get(p["id"], {})
        vendors[p["ref"]] = Vendor(
            vendor_id=p["ref"],
            name=p["name"],
            npwp=p["vat"] or "",
            address=p["street"] or "",
            bank_name=bank.get("bank_name") or "",
            # No trusted account: nothing on an invoice can match, so payment is held.
            bank_account=bank.get("account_number") or "",
        )
    return vendors, {p["id"]: p["ref"] for p in partners}


def fetch_orders(
    client: OdooClient, vendor_by_partner: dict[int, str]
) -> tuple[dict[str, PurchaseOrder], dict[str, GoodsReceipt]]:
    orders = client.search_read(
        "purchase.order",
        [("state", "in", ["purchase", "done"])],
        ["name", "partner_id", "date_order"],
    )
    order_ids = [o["id"] for o in orders]
    lines = client.search_read(
        "purchase.order.line",
        [("order_id", "in", order_ids), ("display_type", "=", False)],
        ["order_id", "product_id", "name", "product_qty", "price_unit", "qty_received"],
        order="order_id, sequence, id",
    )
    product_ids = sorted({_m2o_id(ln["product_id"]) for ln in lines})
    sku = {
        p["id"]: p["default_code"]
        for p in client.search_read(
            "product.product", [("id", "in", product_ids)], ["default_code"]
        )
    }
    pickings = client.search_read(
        "stock.picking",
        [
            ("purchase_id", "in", order_ids),
            ("state", "=", "done"),
            ("picking_type_code", "=", "incoming"),
        ],
        ["name", "purchase_id", "date_done"],
        order="date_done, id",
    )

    lines_by_order: dict[int, list[dict]] = defaultdict(list)
    for ln in lines:
        lines_by_order[_m2o_id(ln["order_id"])].append(ln)
    picks_by_order: dict[int, list[dict]] = defaultdict(list)
    for pk in pickings:
        picks_by_order[_m2o_id(pk["purchase_id"])].append(pk)

    pos: dict[str, PurchaseOrder] = {}
    grs: dict[str, GoodsReceipt] = {}
    for o in orders:
        vendor_id = vendor_by_partner.get(_m2o_id(o["partner_id"]))
        if vendor_id is None:
            continue  # a supplier outside the vendor master; not ours to match
        olines = lines_by_order[o["id"]]
        pos[o["name"]] = PurchaseOrder(
            po_number=o["name"],
            vendor_id=vendor_id,
            date=_date(o["date_order"]),
            lines=[
                POLine(
                    sku=sku[_m2o_id(ln["product_id"])],
                    description=ln["name"],
                    qty=_whole(ln["product_qty"], "PO qty"),
                    unit_price=_whole(ln["price_unit"], "PO unit price"),
                )
                for ln in olines
            ],
        )
        picks = picks_by_order.get(o["id"])
        if picks:
            grs[o["name"]] = GoodsReceipt(
                # One receipt per PO in the synthetic data; with several, the first
                # number identifies the set and the quantities are cumulative.
                gr_number=picks[0]["name"],
                po_number=o["name"],
                date=_date(picks[-1]["date_done"]),
                lines=[
                    GRLine(
                        sku=sku[_m2o_id(ln["product_id"])],
                        qty_received=_whole(ln["qty_received"], "received qty"),
                    )
                    for ln in olines
                ],
            )
    return pos, grs


def fetch_master(client: OdooClient) -> MasterData:
    vendors, vendor_by_partner = fetch_vendors(client)
    pos, grs = fetch_orders(client, vendor_by_partner)
    return MasterData(vendors=vendors, purchase_orders=pos, goods_receipts=grs)
