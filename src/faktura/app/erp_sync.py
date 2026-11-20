"""Worker that posts approved invoices from the outbox to the ERP, and writes approved
vendor bank-account changes to it (before any bill, so a new account is trusted first).

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
from opentelemetry import trace
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from faktura.app import audit, bank_changes
from faktura.app.db import BankChangeRow, ErpOutbox, OutboxStatus, make_engine, make_sessionmaker
from faktura.erp.odoo import OdooError
from faktura.erp.odoo_bills import NeedsAttention, PostedBill

log = logging.getLogger("faktura.erp_sync")
_tracer = trace.get_tracer("faktura.erp_sync")  # one trace per bill: all its Odoo calls
ACTOR = "erp-sync"
MAX_ATTEMPTS = 8
BACKOFF_S = 15  # 15 s, 30 s, 1 min, ... capped at an hour

Poster = Callable[[str, dict], PostedBill]
BankApplier = Callable[[dict], int]  # approved bank change -> ERP bank account id


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _retryable(e: Exception) -> bool:
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, OdooError) and (e.status >= 500 or e.status == 429)


def _unsynced_bank_change(session: Session, payload: dict) -> BankChangeRow | None:
    account = bank_changes.digits((payload.get("reading") or {}).get("bank_account"))
    if not account or not payload.get("vendor_id"):
        return None
    return session.execute(
        select(BankChangeRow).where(
            BankChangeRow.vendor_id == payload["vendor_id"],
            BankChangeRow.new_bank_account == account,
            BankChangeRow.status == "approved",
            BankChangeRow.erp_status.in_(["pending", "needs_attention"]),
        )
    ).scalar()


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

    row.updated_at = _iso(now)
    waiting = _unsynced_bank_change(session, row.payload)
    if waiting is not None:
        # Paid into an account verified in Faktura that Odoo does not trust yet: wait for
        # the bank change instead of failing the bill.
        row.next_attempt_at = _iso(now + dt.timedelta(seconds=BACKOFF_S))
        row.last_error = f"waiting for bank change {waiting.id} to be trusted in the ERP"
        session.commit()
        return OutboxStatus.PENDING.value
    row.attempts += 1
    try:
        with _tracer.start_as_current_span("erp.post_bill") as span:
            span.set_attribute("faktura.invoice_id", row.invoice_id)
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


def sync_bank_change(session: Session, change_id: int, apply: BankApplier) -> str:
    """Write one approved bank change to the ERP. Returns its new erp_status."""
    row = session.execute(
        select(BankChangeRow)
        .where(BankChangeRow.id == change_id, BankChangeRow.erp_status == "pending")
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if row is None:
        return "skipped"  # another worker has it, or it is done
    try:
        with _tracer.start_as_current_span("erp.apply_bank_change") as span:
            span.set_attribute("faktura.bank_change_id", row.id)
            bank_id = apply(bank_changes.view(row))
    except NeedsAttention as e:
        row.erp_status, row.erp_error = "needs_attention", str(e)[:2000]
    except Exception as e:
        if not _retryable(e):
            row.erp_status = "needs_attention"
        row.erp_error = f"{type(e).__name__}: {e}"[:2000]  # retryable: next pass
    else:
        row.erp_status, row.erp_error = "synced", None
        audit.append(
            session,
            row.source_invoice_id,
            ACTOR,
            "bank_change_synced",
            {"bank_change_id": row.id, "vendor_id": row.vendor_id, "odoo_bank_id": bank_id},
        )
    if row.erp_status == "needs_attention":
        audit.append(
            session,
            row.source_invoice_id,
            ACTOR,
            "bank_change_needs_attention",
            {"bank_change_id": row.id, "vendor_id": row.vendor_id, "error": row.erp_error},
        )
    row.updated_at = _iso(_now())
    session.commit()
    return row.erp_status


def drain(
    sessions: sessionmaker[Session], post: Poster, apply: BankApplier | None = None
) -> dict[str, int]:
    """Bank changes first, so a bill paying into a just-verified account finds it trusted."""
    counts: dict[str, int] = {}
    if apply is not None:
        with sessions() as session:
            pending = (
                session.execute(
                    select(BankChangeRow.id)
                    .where(BankChangeRow.erp_status == "pending")
                    .order_by(BankChangeRow.id)
                )
                .scalars()
                .all()
            )
        for change_id in pending:  # each once per pass: a retryable failure waits
            with sessions() as session:
                status = sync_bank_change(session, change_id, apply)
            counts[f"bank_{status}"] = counts.get(f"bank_{status}", 0) + 1
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

    from faktura.app.observability import init_tracing, trace_engine
    from faktura.erp.odoo import OdooClient
    from faktura.erp.odoo_banks import apply_bank_change
    from faktura.erp.odoo_bills import post_bill

    load_dotenv()
    init_tracing("faktura-erp-sync")  # spans for the Odoo calls, when OTLP is configured
    client = OdooClient.from_env()
    engine = make_engine()
    trace_engine(engine)
    sessions = make_sessionmaker(engine)

    def post(invoice_id: str, payload: dict) -> PostedBill:
        return post_bill(client, invoice_id, payload)

    def apply(change: dict) -> int:
        return apply_bank_change(client, change)

    while True:
        t0 = time.perf_counter()
        counts = drain(sessions, post, apply)
        if counts:
            log.info("processed %s in %.1fs", counts, time.perf_counter() - t0)
        if args.once:
            print(counts)
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
