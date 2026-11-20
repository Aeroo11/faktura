"""Load the synthetic master data (vendors, POs, goods receipts) into the local Odoo.

Runs inside `odoo shell` as the admin, the way purchasing and warehouse staff would
have entered this data in a real ERP. Faktura itself only reads it, through the
JSON-2 API as the integration user. Idempotent: records that exist are skipped.

    docker compose --profile erp run --rm -T \
        -v ./scripts:/faktura-scripts:ro -v ./data/synth/v2:/faktura-data:ro \
        --entrypoint sh odoo -c 'odoo shell ... < /faktura-scripts/odoo_seed.py'
"""

# ruff: noqa: F821  (`env` is provided by odoo shell)
import datetime
import json
from pathlib import Path

DATA = Path("/faktura-data")
# Indonesia's localization ships PPN as "12% (Non-Luxury Good)": 12% on a DPP of
# 11/12, an effective 11%, which is the rate the synthetic invoices use.
PPN_NAME = "12% (Non-Luxury Good)"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


vendors = load("vendors.json")
pos = load("purchase_orders.json")
grs = {g["po_number"]: g for g in load("goods_receipts.json")}

company = env.company
admin = env.ref("base.user_admin")
ppn = env["account.tax"].search(
    [("name", "=", PPN_NAME), ("type_tax_use", "=", "purchase"), ("company_id", "=", company.id)],
    limit=1,
)
assert ppn and ppn.amount == 11.0, "Indonesian PPN tax not found; run odoo_setup.py first"

# Vendors, keyed by our vendor id in `ref`, NPWP in `vat`, one trusted bank account.
Partner = env["res.partner"]
partners = {}
for v in vendors:
    p = Partner.search([("ref", "=", v["vendor_id"])], limit=1)
    if not p:
        p = Partner.create(
            {
                "name": v["name"],
                "is_company": True,
                "ref": v["vendor_id"],
                "vat": v["npwp"],
                "street": v["address"],
                "phone": v.get("phone") or False,
                "country_id": env.ref("base.id").id,
                "supplier_rank": 1,
            }
        )
        env["res.partner.bank"].create(
            {
                "partner_id": p.id,
                "account_number": v["bank_account"],
                "bank_name": v["bank_name"],
            }
        )
    if v.get("phone") and not p.phone:  # seeded before vendors had a phone on file
        p.phone = v["phone"]
    # The onboarding account counts as verified. Odoo refuses to let OdooBot (which
    # `odoo shell` runs as) trust a bank account, so a named admin user does it.
    account = v["bank_account"]
    onboarded = p.bank_ids.filtered(lambda b, account=account: b.account_number == account)
    if onboarded and not onboarded.allow_out_payment:
        onboarded.with_user(admin).allow_out_payment = True
    # Re-running the seed resets the vendor to its onboarding account, e.g. after the
    # bank-change demo trusted a new one.
    for other in (p.bank_ids - onboarded).filtered("allow_out_payment"):
        other.with_user(admin).allow_out_payment = False
    partners[v["vendor_id"]] = p

# Products, keyed by SKU. The shortest description seen becomes the product name.
names = {}
for po in pos:
    for ln in po["lines"]:
        d = ln["description"]
        if ln["sku"] not in names or len(d) < len(names[ln["sku"]]):
            names[ln["sku"]] = d
Product = env["product.product"]
products = {}
for sku, name in sorted(names.items()):
    pr = Product.search([("default_code", "=", sku)], limit=1)
    if not pr:
        pr = Product.create(
            {
                "name": name,
                "default_code": sku,
                "type": "consu",
                "is_storable": True,
                "purchase_method": "receive",  # bill what was received, not ordered
                "supplier_taxes_id": [(6, 0, ppn.ids)],
            }
        )
    products[sku] = pr

PO = env["purchase.order"]
created = 0
for po in pos:
    if PO.search_count([("name", "=", po["po_number"])]):
        continue
    when = datetime.datetime.fromisoformat(po["date"] + "T09:00:00")
    order = PO.create(
        {
            "name": po["po_number"],
            "partner_id": partners[po["vendor_id"]].id,
            "date_order": when,
            "order_line": [
                (
                    0,
                    0,
                    {
                        "product_id": products[ln["sku"]].id,
                        "name": ln["description"],
                        "product_qty": ln["qty"],
                        "price_unit": ln["unit_price"],
                        "tax_ids": [(6, 0, ppn.ids)],
                        "date_planned": when,
                    },
                )
                for ln in po["lines"]
            ],
        }
    )
    order.button_confirm()
    order.date_approve = when

    gr = grs.get(po["po_number"])
    picking = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))[:1]
    if gr and picking:
        received = {ln["sku"]: ln["qty_received"] for ln in gr["lines"]}
        for move in picking.move_ids:
            move.quantity = received.get(move.product_id.default_code, 0)
            move.picked = True
        if any(received.values()):
            picking.name = gr["gr_number"]
            # Partial receipts close the PO line short instead of leaving a backorder.
            picking.with_context(
                skip_backorder=True, picking_ids_not_to_backorder=picking.ids
            ).button_validate()
            picking.date_done = datetime.datetime.fromisoformat(gr["date"] + "T10:00:00")
        else:
            picking.action_cancel()
    created += 1
    if created % 50 == 0:
        env.cr.commit()
        print(f"... {created} purchase orders")

env.cr.commit()
print(
    f"vendors={len(partners)} products={len(products)} new_pos={created} "
    f"total_pos={PO.search_count([])} receipts_done="
    f"{env['stock.picking'].search_count([('state', '=', 'done')])}"
)
