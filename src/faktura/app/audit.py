"""Tamper-evident audit trail.

Every event stores sha256(previous hash + canonical event). Changing, deleting, or
reordering any past row breaks every hash after it, which `verify_chain` reports. On
Postgres the migration additionally rejects UPDATE and DELETE on the table.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from faktura.app.db import AuditEvent, AuditHead

GENESIS = "0" * 64


def _canonical(
    seq: int,
    invoice_id: str | None,
    at: str,
    actor: str,
    action: str,
    payload: dict[str, Any],
    prev_hash: str,
) -> bytes:
    body = {
        "seq": seq,
        "invoice_id": invoice_id,
        "at": at,
        "actor": actor,
        "action": action,
        "payload": payload,
        "prev": prev_hash,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def event_hash(e: AuditEvent) -> str:
    return hashlib.sha256(
        _canonical(e.seq, e.invoice_id, e.at, e.actor, e.action, e.payload, e.prev_hash)
    ).hexdigest()


def append(
    session: Session, invoice_id: str | None, actor: str, action: str, payload: dict[str, Any]
) -> AuditEvent:
    """Append one event. Call inside the transaction that makes the change it records."""
    head = session.execute(select(AuditHead).where(AuditHead.id == 1).with_for_update()).scalar()
    if head is None:
        head = AuditHead(id=1, last_seq=0, last_hash=GENESIS)
        session.add(head)
        session.flush()
    payload = json.loads(json.dumps(payload, default=str))  # store exactly what is hashed
    at = dt.datetime.now(dt.UTC).isoformat(timespec="microseconds")
    event = AuditEvent(
        seq=head.last_seq + 1,
        invoice_id=invoice_id,
        at=at,
        actor=actor,
        action=action,
        payload=payload,
        prev_hash=head.last_hash,
        hash="",
    )
    event.hash = event_hash(event)
    head.last_seq, head.last_hash = event.seq, event.hash
    session.add(event)
    return event


def verify_chain(session: Session) -> dict[str, Any]:
    prev = GENESIS
    n = 0
    for e in session.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars():
        n += 1
        if e.seq != n or e.prev_hash != prev or e.hash != event_hash(e):
            return {"ok": False, "events": n, "first_broken_seq": e.seq}
        prev = e.hash
    head = session.get(AuditHead, 1)
    if head is not None and (head.last_seq != n or head.last_hash != prev):
        return {"ok": False, "events": n, "first_broken_seq": n + 1}  # rows removed at the end
    return {"ok": True, "events": n, "first_broken_seq": None}
