"""`faktura-seed`: load the synthetic dataset and saved model readings into the database.

Invoices are ingested in arrival order through the same service code the API uses, so
the demo queue is exactly what the engine decides. No model calls are made.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from sqlalchemy import func, select

from faktura.app import service
from faktura.app.api import load_context
from faktura.app.db import Base, InvoiceRow, make_engine, make_sessionmaker
from faktura.evals.extraction import load_labels
from faktura.evals.matching_eval import load_readings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faktura-seed")
    parser.add_argument("--data", type=Path, default=Path("data/synth/v2"))
    parser.add_argument(
        "--runs",
        type=Path,
        nargs="+",
        required=True,
        help="extraction run dirs with raw/ readings, first match wins",
    )
    parser.add_argument(
        "--create-schema",
        action="store_true",
        help="create tables directly (SQLite/dev); use Alembic for Postgres",
    )
    args = parser.parse_args(argv)

    engine = make_engine()
    if args.create_schema:
        Base.metadata.create_all(engine)
    sessions = make_sessionmaker(engine)
    ctx = load_context(args.data)
    readings = load_readings(args.runs)
    labels = load_labels(args.data)

    t0 = time.perf_counter()
    with sessions() as session:
        if session.execute(select(func.count()).select_from(InvoiceRow)).scalar():
            parser.error("database already has invoices; seed into an empty database")
        for lb in labels:  # file order = arrival order
            if lb.invoice_id not in readings:
                continue
            pdf = (args.data / "invoices" / f"{lb.invoice_id}.pdf").resolve()
            service.ingest(
                session,
                ctx,
                lb.invoice_id,
                str(pdf),
                readings[lb.invoice_id].model_dump(mode="json"),
            )
            session.commit()
        print(service.summary(session))
    print(f"seeded {len(readings)} invoices in {time.perf_counter() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
