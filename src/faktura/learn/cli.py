"""`faktura-learn`: fit the misread model from the corrections recorded in the audit trail.

    uv run faktura-learn --out models/misread.json [--export corrections.jsonl]

Training data is read only from `field_corrected` audit events, and only if the audit
hash chain verifies: a model that changes how payments are triaged must not be trained
on history someone could have edited. The output records its provenance (event count,
time range, chain status) and a content-derived version, which every decision made with
it carries in its `decided` audit event.

Retraining is a deliberate batch step, not online learning, so the same invoice always
gets the same decision from the same model version.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from faktura.app import audit
from faktura.app.db import AuditEvent, make_engine, make_sessionmaker
from faktura.learn.misread import DIGIT_FIELDS, Correction, MisreadModel


def corrections_from_audit(session) -> tuple[list[Correction], list[dict]]:
    events = session.execute(
        select(AuditEvent).where(AuditEvent.action == "field_corrected").order_by(AuditEvent.seq)
    ).scalars()
    corrections, rows = [], []
    for e in events:
        field = e.payload.get("field")
        if field not in DIGIT_FIELDS:
            continue
        c = Correction(field, e.payload.get("model_read"), e.payload.get("new"))
        corrections.append(c)
        rows.append(
            {
                "invoice_id": e.invoice_id,
                "field": field,
                "model_read": c.model_read,
                "corrected": c.corrected,
                "actor": e.actor,
                "at": e.at,
            }
        )
    return corrections, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("models/misread.json"))
    parser.add_argument("--export", type=Path, help="also write the corrections as JSONL")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    with make_sessionmaker(make_engine())() as session:
        chain = audit.verify_chain(session)
        if not chain["ok"]:
            raise SystemExit(f"audit chain broken at seq {chain['first_broken_seq']}; not training")
        corrections, rows = corrections_from_audit(session)

    model = MisreadModel.fit(corrections)
    model.trained_on = {
        "source": "audit field_corrected",
        "events": len(rows),
        "first_at": rows[0]["at"] if rows else None,
        "last_at": rows[-1]["at"] if rows else None,
        "audit_chain_events": chain["events"],
    }
    model.save(args.out)
    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    known = [e for e in model.edit_counts if model.known(e)]
    print(f"{model.version}: {model.n_corrections} corrections, known edits {known} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
