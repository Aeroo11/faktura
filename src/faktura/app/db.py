"""Database models and session setup (SQLAlchemy 2).

Postgres in production (docker-compose), SQLite for tests and quick local runs. The
models use only portable types; the Postgres-only guard that blocks UPDATE/DELETE on the
audit table lives in the Alembic migration.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import JSON, ForeignKey, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_URL = "sqlite:///data/faktura.db"


class State(StrEnum):
    """Where an invoice is in the human workflow (the engine's Decision is separate)."""

    APPROVED = "approved"  # payable: auto-approved, or approved by a checker
    NEEDS_ACTION = "needs_action"  # waiting for AP staff (maker)
    AWAITING_APPROVAL = "awaiting_approval"  # maker done; waiting for a different checker
    REJECTED = "rejected"


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class InvoiceRow(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(unique=True, index=True)  # arrival order
    pdf_path: Mapped[str] = mapped_column(String(512))
    state: Mapped[str] = mapped_column(String(32), index=True)
    decision: Mapped[str] = mapped_column(String(32), index=True)
    reasons: Mapped[list[Any]]  # [{code, status, message}]
    reading: Mapped[dict[str, Any]]  # current ExtractedInvoice, incl. human corrections
    model_reading: Mapped[dict[str, Any]]  # what the model read; never overwritten
    human_verified: Mapped[list[Any]]  # fields a person confirmed or corrected
    makers: Mapped[list[Any]]  # users who changed or submitted this invoice
    vendor_id: Mapped[str | None] = mapped_column(String(32), index=True)
    total: Mapped[int | None]
    auto_approved: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(default=1)  # optimistic concurrency


class AuditEvent(Base):
    """Append-only. Each row stores the hash of the previous row (hash chain)."""

    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id"), index=True)
    at: Mapped[str] = mapped_column(String(40))  # ISO 8601 UTC, exactly as hashed
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]]
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class AuditHead(Base):
    """Single row holding the chain head; locked while appending so the chain is linear."""

    __tablename__ = "audit_head"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_seq: Mapped[int] = mapped_column(default=0)
    last_hash: Mapped[str] = mapped_column(String(64))


def make_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    if url == "sqlite://":  # in-memory, shared across threads (tests)
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as session:
        yield session
