import json
import os
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from faktura.erp.odoo import OdooClient, OdooError
from faktura.erp.odoo_master import fetch_master
from faktura.master import MasterData

ROOT = Path(__file__).resolve().parents[1]


def client_for(handler) -> OdooClient:
    return OdooClient("http://odoo.test", "k3y", db="erp", transport=httpx.MockTransport(handler))


def test_call_uses_json2_shape_and_bearer_key():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(
            path=req.url.path,
            auth=req.headers["authorization"],
            db=req.headers["x-odoo-database"],
            body=json.loads(req.content),
        )
        return httpx.Response(200, json=[{"id": 1}])

    out = client_for(handler).call("res.partner", "read", ids=[1], fields=["name"])
    assert out == [{"id": 1}]
    assert seen == {
        "path": "/json/2/res.partner/read",
        "auth": "bearer k3y",
        "db": "erp",
        "body": {"ids": [1], "fields": ["name"]},
    }


def test_error_body_becomes_odoo_error():
    def handler(req):
        return httpx.Response(
            401, json={"name": "werkzeug.exceptions.Unauthorized", "message": "Invalid apikey"}
        )

    with pytest.raises(OdooError) as e:
        client_for(handler).call("res.partner", "search", domain=[])
    assert e.value.status == 401 and e.value.message == "Invalid apikey"


def test_reads_retry_but_writes_do_not():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(503, text="busy") if len(calls) == 1 else httpx.Response(200, json=[])

    assert client_for(handler).search_read("res.partner", [], ["name"]) == []
    assert len(calls) == 2

    calls.clear()
    with pytest.raises(OdooError):  # a write might have landed; never resend blindly
        client_for(handler).call("account.move", "create", vals_list=[{}])
    assert len(calls) == 1


def fake_odoo(banks: list[dict]):
    """Canned search_read results for one vendor with one PO, received short."""
    data = {
        "res.partner": [
            {"id": 7, "ref": "V001", "name": "PT A", "vat": "0" * 16, "street": "Jl. X"}
        ],
        "res.partner.bank": banks,
        "purchase.order": [
            {
                "id": 3,
                "name": "PO/1",
                "partner_id": [7, "PT A"],
                "date_order": "2025-06-18 09:00:00",
            }
        ],
        "purchase.order.line": [
            {
                "id": 9,
                "order_id": [3, "PO/1"],
                "product_id": [4, "Kertas"],
                "name": "Kertas A4",
                "product_qty": 10.0,
                "price_unit": 52000.0,
                "qty_received": 8.0,
            }
        ],
        "product.product": [{"id": 4, "default_code": "ATK-001"}],
        "stock.picking": [
            {
                "id": 5,
                "name": "GR/1",
                "purchase_id": [3, "PO/1"],
                "date_done": "2025-07-01 10:00:00",
            }
        ],
    }

    def handler(req):
        model = req.url.path.split("/")[3]
        return httpx.Response(200, json=data[model])

    return client_for(handler)


def test_fetch_master_maps_odoo_records():
    md = fetch_master(
        fake_odoo(
            [{"id": 1, "partner_id": [7, "PT A"], "account_number": "123", "bank_name": "BNI"}]
        )
    )
    v = md.vendors["V001"]
    assert (v.npwp, v.bank_name, v.bank_account) == ("0" * 16, "BNI", "123")
    po = md.purchase_orders["PO/1"]
    assert (po.vendor_id, str(po.date), po.lines[0].sku, po.lines[0].unit_price) == (
        "V001",
        "2025-06-18",
        "ATK-001",
        52000,
    )
    gr = md.goods_receipts["PO/1"]
    assert (gr.gr_number, gr.lines[0].qty_received) == ("GR/1", 8)


def test_vendor_without_trusted_account_has_no_payable_account():
    # The query only returns trusted accounts; none means no invoice can match the master.
    v = fetch_master(fake_odoo([])).vendors["V001"]
    assert v.bank_account == "" and v.bank_name == ""


def _odoo_env() -> dict:
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    return {k: env.get(k) for k in ("ODOO_URL", "ODOO_API_KEY", "ODOO_DB")}


def _odoo_up(url: str | None) -> bool:
    if not url:
        return False
    try:
        return httpx.get(f"{url}/web/login", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not _odoo_up(_odoo_env()["ODOO_URL"]), reason="local Odoo not running")
@pytest.mark.skipif(not (ROOT / "data/synth/v2").exists(), reason="synthetic v2 data not built")
def test_master_from_odoo_is_identical_to_the_seeded_files():
    """Parity: after scripts/odoo_seed.py, Odoo is a drop-in source of the same master data."""
    env = _odoo_env()
    odoo = fetch_master(OdooClient(env["ODOO_URL"], env["ODOO_API_KEY"], db=env["ODOO_DB"]))
    files = MasterData.load(ROOT / "data/synth/v2")
    assert odoo.vendors == files.vendors
    assert odoo.purchase_orders == files.purchase_orders
    assert odoo.goods_receipts == files.goods_receipts


def test_cached_master_refetches_after_ttl():
    from faktura.master import CachedMaster

    now = [0.0]
    fetched = []

    def fetch():
        fetched.append(now[0])
        return MasterData(vendors={}, purchase_orders={}, goods_receipts={})

    src = CachedMaster(fetch, name="odoo", ttl_s=30, clock=lambda: now[0])
    src.snapshot()
    now[0] = 29.9
    src.snapshot()
    now[0] = 30.0
    src.snapshot()
    assert fetched == [0.0, 30.0]
