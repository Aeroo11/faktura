"""One-time setup of the local Odoo, run inside `odoo shell` (see docs/erp.md).

Sets up the buying company (fictitious, Indonesian, IDR, Indonesian chart of accounts)
and a dedicated integration user, then prints an API key for that user on a single
line prefixed with FAKTURA_ODOO_KEY=. Faktura never uses the admin account.

    docker compose --profile erp run --rm -T odoo odoo shell -d faktura_erp --no-http \
        < scripts/odoo_setup.py
"""

# ruff: noqa: F821  (`env` is provided by odoo shell)
import datetime

BOT_LOGIN = "faktura-bot"
KEY_DAYS = 90  # Odoo's documented maximum for user-created keys; renew via docs/erp.md

company = env.company
idr = env.ref("base.IDR")
idr.active = True
company.write(
    {
        "name": "PT Pembeli Contoh (SINTETIS)",
        "country_id": env.ref("base.id").id,
        "currency_id": idr.id,
    }
)
if company.chart_template != "id":
    env["account.chart.template"].try_loading("id", company, install_demo=False)

# Idempotency key for bills posted by Faktura: one bill per Faktura invoice id.
if not env["ir.model.fields"].search(
    [("model", "=", "account.move"), ("name", "=", "x_faktura_id")]
):
    env["ir.model.fields"].create(
        {
            "model_id": env["ir.model"]._get("account.move").id,
            "name": "x_faktura_id",
            "field_description": "Faktura invoice id",
            "ttype": "char",
            "index": True,
            "copied": False,
        }
    )

groups = [
    "base.group_user",
    "purchase.group_purchase_user",
    "stock.group_stock_user",
    "account.group_account_invoice",
]
bot = env["res.users"].with_context(active_test=False).search([("login", "=", BOT_LOGIN)])
if not bot:
    bot = env["res.users"].create({"name": "Faktura integration", "login": BOT_LOGIN})
bot.write({"active": True, "group_ids": [(4, env.ref(g).id) for g in groups]})

# Replace earlier keys so re-running setup leaves exactly one valid key.
env["res.users.apikeys"].sudo().search([("user_id", "=", bot.id)]).unlink()
expires = datetime.datetime.now() + datetime.timedelta(days=KEY_DAYS)
key = env["res.users.apikeys"].with_user(bot).sudo()._generate("rpc", "faktura", expires)
env.cr.commit()

print(f"chart={company.chart_template} currency={company.currency_id.name} bot_id={bot.id}")
print(f"FAKTURA_ODOO_KEY={key}")
