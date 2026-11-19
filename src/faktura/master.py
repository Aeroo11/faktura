"""Master data the AP team already trusts: vendors, purchase orders, goods receipts."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

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


class MasterSource(Protocol):
    """Where master data comes from. `snapshot()` must not be mutated by callers."""

    name: str

    def snapshot(self) -> MasterData: ...


@dataclass
class StaticMaster:
    """Master data fixed at startup: the synthetic files, or a test fixture."""

    md: MasterData
    name: str = "files"

    def snapshot(self) -> MasterData:
        return self.md


class CachedMaster:
    """Master data fetched from a live system and reused for `ttl_s` seconds.

    The TTL bounds how stale a decision can be: a bank account untrusted in the ERP
    stops matching within `ttl_s`. If a refresh fails, the error propagates: deciding
    on data of unknown age is worse than not deciding.
    """

    def __init__(
        self,
        fetch: Callable[[], MasterData],
        name: str,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.name = name
        self._fetch = fetch
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._md: MasterData | None = None
        self._at = 0.0

    def snapshot(self) -> MasterData:
        with self._lock:
            now = self._clock()
            if self._md is None or now - self._at >= self._ttl:
                self._md = self._fetch()
                self._at = now
            return self._md
