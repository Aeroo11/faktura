"""Check that the PO-scoped billing replay decides exactly like the full replay.

Re-decides every invoice in the database both ways (read-only) and reports any
difference and the mean time per decision. See docs/experiments.md (2026-11-20).

    DATABASE_URL=... uv run python scripts/ledger_equivalence.py
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

from sqlalchemy import select

from faktura.app import service
from faktura.app.api import load_context
from faktura.app.db import InvoiceRow, make_engine, make_sessionmaker
from faktura.matching import decide
from faktura.validate import resolve_vendor


def main() -> int:
    ctx = load_context(Path("data/synth/v2"), master="files")
    sessions = make_sessionmaker(make_engine())
    differing = 0
    spent = {"full": 0.0, "scoped": 0.0}
    with sessions() as s:
        rows = s.execute(select(InvoiceRow).order_by(InvoiceRow.seq)).scalars().all()
        snap = service.master_snapshot(s, ctx)
        for row in rows:
            ext = service._effective_reading(row)
            vendor, _ = resolve_vendor(ext, snap)
            vid = vendor.vendor_id if vendor else None
            md = copy.copy(snap)
            md.approved_invoice_numbers = (
                {vid: service._approved_numbers(s, vid, row.id)} if vid else {}
            )
            out = {}
            for mode, po in (("full", None), ("scoped", ext.po_number or "")):
                t0 = time.perf_counter()
                ledger = service.build_ledger(s, row.seq, md, po_number=po)
                d = decide(
                    row.id,
                    ext,
                    md,
                    ledger,
                    ctx.vat_bps,
                    misread=ctx.misread,
                    verified=set(row.human_verified),
                )
                spent[mode] += time.perf_counter() - t0
                out[mode] = (d.status, [r.model_dump() for r in d.reasons])
            if out["full"] != out["scoped"]:
                differing += 1
                print("DIFF", row.id, out["full"][0], out["scoped"][0])
    n = len(rows)
    print(f"invoices {n}, decisions that differ: {differing}")
    print(
        f"mean per decision: full {1000 * spent['full'] / n:.0f} ms, "
        f"scoped {1000 * spent['scoped'] / n:.0f} ms"
    )
    return 1 if differing else 0


if __name__ == "__main__":
    raise SystemExit(main())
