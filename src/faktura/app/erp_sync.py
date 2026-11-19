"""Worker that posts approved invoices from the outbox to the ERP.

    uv run faktura-erp-sync            # keep running, poll every few seconds
    uv run faktura-erp-sync --once     # process what is due, then exit

Each outbox row is locked while it is processed (FOR UPDATE SKIP LOCKED on Postgres), so
several workers can run without posting the same bill twice; the ERP-side lookup by
invoice id covers the case where a worker dies after Odoo committed but before we did.
Retryable failures (network, ERP unavailable) back off exponentially; anything a
person must fix becomes `needs_attention` and shows on the invoice.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import time
from collections.abc import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from faktura.app import audit
from faktura.app.db import ErpOutbox, OutboxStatus, make_engine, make_sessionmaker
from faktura.erp.odoo import OdooError
from faktura.erp.odoo_bills import NeedsAttention, PostedBill

log = logging.getLogger("faktura.erp_sync")
ACTOR = "erp-sync"
MAX_ATTEMPTS = 8
BACKOFF_S = 15  # 15 s, 30 s, 1 min, ... capped at an hour

Poster = Callable[[str, dict], PostedBill]


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _retryable(e: Exception) -> bool:
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, OdooError) and (e.status >= 500 or e.status == 429)


def process_one(session: Session, post: Poster, now: dt.datetime | None = None) -> str | None:
    """Post the next due outbox row. Returns its new status, or None if nothing is due."""
    now = now or _now()
    row = session.execute(
        select(ErpOutbox)
        .where(ErpOutbox.status == OutboxStatus.PENDING, ErpOutbox.next_attempt_at <= _iso(now))
        .order_by(ErpOutbox.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if row is None:
        return None

    row.attempts += 1
    row.updated_at = _iso(now)
    try:
        bill = post(row.invoice_id, row.payload)
    except NeedsAttention as e:
        row.status = OutboxStatus.NEEDS_ATTENTION.value
        row.last_error = str(e)[:2000]
        audit.append(session, row.invoice_id, ACTOR, "erp_needs_attention", {"error": str(e)})
    except Exception as e:
        if not _retryable(e) or row.attempts >= MAX_ATTEMPTS:
            row.status = OutboxStatus.NEEDS_ATTENTION.value
            row.last_error = f"{type(e).__name__}: {e}"[:2000]
            audit.append(
                session, row.invoice_id, ACTOR, "erp_needs_attention", {"error": row.last_error}
            )
        else:
            delay = min(BACKOFF_S * 2 ** (row.attempts - 1), 3600)
            row.next_attempt_at = _iso(now + dt.timedelta(seconds=delay))
            row.last_error = f"{type(e).__name__}: {e}"[:2000]
            log.warning("retrying %s in %ss: %s", row.invoice_id, delay, row.last_error)
    else:
        row.status = OutboxStatus.POSTED.value
        row.erp_id, row.erp_name, row.last_error = bill.move_id, bill.name, None
        audit.append(
            session,
            row.invoice_id,
            ACTOR,
            "erp_bill_posted",
            {"erp": "odoo", "move_id": bill.move_id, "name": bill.name, "created": bill.created},
        )
    session.commit()
    return row.status


def drain(sessions: sessionmaker[Session], post: Poster) -> dict[str, int]:
    counts: dict[str, int] = {}
    while True:
        with sessions() as session:
            status = process_one(session, post)
        if status is None:
            return counts
        counts[status] = counts.get(status, 0) + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="process what is due, then exit")
    parser.add_argument("--interval", type=float, default=5.0, help="poll interval, seconds")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise

    from dotenv import load_dotenv

    from faktura.erp.odoo import OdooClient
    from faktura.erp.odoo_bills import post_bill

    load_dotenv()
    client = OdooClient.from_env()
    sessions = make_sessionmaker(make_engine())

    def post(invoice_id: str, payload: dict) -> PostedBill:
        return post_bill(client, invoice_id, payload)

    while True:
        t0 = time.perf_counter()
        counts = drain(sessions, post)
        if counts:
            log.info("processed %s in %.1fs", counts, time.perf_counter() - t0)
        if args.once:
            print(counts)
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
