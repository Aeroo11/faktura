"""Anomaly injectors. Each returns a new invoice that breaks exactly one rule.

Totals are always recomputed so the printed document stays internally consistent,
except for VAT_MISCALC where the inconsistency *is* the anomaly.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from faktura.schemas import (
    AnomalyType,
    GoodsReceipt,
    Invoice,
    InvoiceLine,
    Vendor,
    compute_vat,
)


def recompute_totals(inv: Invoice, vat_bps: int) -> Invoice:
    lines = [ln.model_copy(update={"amount": ln.qty * ln.unit_price}) for ln in inv.lines]
    dpp = sum(ln.amount for ln in lines)
    vat = compute_vat(dpp, vat_bps)
    return inv.model_copy(update={"lines": lines, "dpp": dpp, "vat": vat, "total": dpp + vat})


def _replace_line(inv: Invoice, idx: int, line: InvoiceLine) -> Invoice:
    lines = list(inv.lines)
    lines[idx] = line
    return inv.model_copy(update={"lines": lines})


def price_mismatch(
    inv: Invoice, vendor: Vendor, gr: GoodsReceipt, rng: random.Random, vat_bps: int
) -> tuple[Invoice, str]:
    idx = rng.randrange(len(inv.lines))
    line = inv.lines[idx]
    pct = rng.uniform(0.02, 0.15)
    # Round up to the next Rp100 so the increase is never rounded away.
    new_price = -(-int(line.unit_price * (1 + pct)) // 100) * 100
    new_price = max(new_price, line.unit_price + 100)
    out = _replace_line(inv, idx, line.model_copy(update={"unit_price": new_price}))
    return recompute_totals(out, vat_bps), f"{line.sku}: {line.unit_price} -> {new_price}"


def qty_over_received(
    inv: Invoice, vendor: Vendor, gr: GoodsReceipt, rng: random.Random, vat_bps: int
) -> tuple[Invoice, str]:
    idx = rng.randrange(len(inv.lines))
    line = inv.lines[idx]
    extra = rng.randint(1, 3)
    out = _replace_line(inv, idx, line.model_copy(update={"qty": line.qty + extra}))
    return recompute_totals(out, vat_bps), f"{line.sku}: qty +{extra}"


def npwp_mismatch(
    inv: Invoice, vendor: Vendor, gr: GoodsReceipt, rng: random.Random, vat_bps: int
) -> tuple[Invoice, str]:
    digits = list(inv.vendor_npwp)
    pos = rng.randrange(len(digits))
    digits[pos] = rng.choice([d for d in "0123456789" if d != digits[pos]])
    return inv.model_copy(update={"vendor_npwp": "".join(digits)}), f"digit {pos} changed"


def vat_miscalc(
    inv: Invoice, vendor: Vendor, gr: GoodsReceipt, rng: random.Random, vat_bps: int
) -> tuple[Invoice, str]:
    # Typical real mistake: applying the nominal 12% instead of the effective rate.
    wrong_bps = 1200 if vat_bps != 1200 else 1100
    vat = compute_vat(inv.dpp, wrong_bps)
    return (
        inv.model_copy(update={"vat": vat, "total": inv.dpp + vat}),
        f"vat at {wrong_bps} bps",
    )


def bank_account_changed(
    inv: Invoice, vendor: Vendor, gr: GoodsReceipt, rng: random.Random, vat_bps: int
) -> tuple[Invoice, str]:
    while True:
        acct = "".join(rng.choice("0123456789") for _ in range(len(vendor.bank_account)))
        if acct != vendor.bank_account:
            break
    return inv.model_copy(update={"bank_account": acct}), "bank account differs from master"


Injector = Callable[[Invoice, Vendor, GoodsReceipt, random.Random, int], tuple[Invoice, str]]

# DUPLICATE_INVOICE is handled by the generator, since it needs earlier invoices.
INJECTORS: dict[AnomalyType, Injector] = {
    AnomalyType.PRICE_MISMATCH: price_mismatch,
    AnomalyType.QTY_OVER_RECEIVED: qty_over_received,
    AnomalyType.NPWP_MISMATCH: npwp_mismatch,
    AnomalyType.VAT_MISCALC: vat_miscalc,
    AnomalyType.BANK_ACCOUNT_CHANGED: bank_account_changed,
}
