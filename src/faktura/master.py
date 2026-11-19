"""Master data the AP team already trusts: vendors, purchase orders, goods receipts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from faktura.schemas import GoodsReceipt, PurchaseOrder, Vendor


def _load(path: Path, model):
    return [model.model_validate(x) for x in json.loads(path.read_text(encoding="utf-8"))]


@dataclass
class MasterData:
    vendors: dict[str, Vendor]
    purchase_orders: dict[str, PurchaseOrder]
    goods_receipts: dict[str, GoodsReceipt]  # keyed by po_number
    # Invoice numbers of previously approved invoices per vendor, used to learn the
    # shape of each vendor's numbering (e.g. "AAA/A999/9999/9999").
    approved_invoice_numbers: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path) -> MasterData:
        vendors = _load(data_dir / "vendors.json", Vendor)
        pos = _load(data_dir / "purchase_orders.json", PurchaseOrder)
        grs = _load(data_dir / "goods_receipts.json", GoodsReceipt)
        return cls(
            vendors={v.vendor_id: v for v in vendors},
            purchase_orders={p.po_number: p for p in pos},
            goods_receipts={g.po_number: g for g in grs},
        )
