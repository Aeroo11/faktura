"""HTTP API for the review queue. Translates requests; all rules live in service.py.

Authentication is a placeholder: the caller states who they are in X-User / X-Role
headers. That is acceptable only for local development and demos; Keycloak (OIDC) is
planned for phase 2. Never expose this API with header auth.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from faktura.app import audit, service
from faktura.app.db import AuditEvent, InvoiceRow, make_engine, make_sessionmaker
from faktura.extract.pdf import render_first_page_png
from faktura.master import MasterData
from faktura.synth.config import load_config

DEFAULT_DATA_DIR = "data/synth/v2"


def load_context(data_dir: Path) -> service.Context:
    return service.Context(
        md=MasterData.load(data_dir), vat_bps=load_config(data_dir / "config.yaml").vat_bps
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "sessions"):
        app.state.sessions = make_sessionmaker(make_engine())
    if not hasattr(app.state, "ctx"):
        app.state.ctx = load_context(Path(os.environ.get("FAKTURA_DATA_DIR", DEFAULT_DATA_DIR)))
    yield


app = FastAPI(title="Faktura review queue", lifespan=lifespan)


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


def get_ctx(request: Request) -> service.Context:
    return request.app.state.ctx


def get_user(x_user: Annotated[str, Header()], x_role: Annotated[str, Header()]) -> service.User:
    """DEV ONLY: trusts the caller's headers. Replaced by OIDC in phase 2."""
    if x_role not in service.ROLES:
        raise HTTPException(403, f"unknown role {x_role!r}")
    return service.User(name=x_user, role=x_role)


SessionDep = Annotated[Session, Depends(get_session)]
CtxDep = Annotated[service.Context, Depends(get_ctx)]
UserDep = Annotated[service.User, Depends(get_user)]


def _run(session: Session, fn, *args, **kwargs) -> InvoiceRow:
    """Run one service action in a transaction and map domain errors to HTTP codes."""
    try:
        row = fn(session, *args, **kwargs)
        session.commit()
        return row
    except service.NotFound as e:
        session.rollback()
        raise HTTPException(404, f"invoice {e} not found") from e
    except service.Forbidden as e:
        session.rollback()
        raise HTTPException(403, str(e)) from e
    except service.WorkflowError as e:
        session.rollback()
        raise HTTPException(409, str(e)) from e
    except service.InvalidValue as e:
        session.rollback()
        raise HTTPException(422, str(e)) from e


def _view(row: InvoiceRow, detail: bool = False) -> dict[str, Any]:
    out = {
        "id": row.id,
        "seq": row.seq,
        "state": row.state,
        "decision": row.decision,
        "vendor_id": row.vendor_id,
        "vendor_name": row.reading.get("vendor_name"),
        "invoice_number": row.reading.get("invoice_number"),
        "total": row.total,
        "auto_approved": row.auto_approved,
        "version": row.version,
        "reason_codes": sorted({r["code"] for r in row.reasons}),
    }
    if detail:
        out |= {
            "reasons": row.reasons,
            "reading": row.reading,
            "model_reading": row.model_reading,
            "human_verified": row.human_verified,
            "makers": row.makers,
        }
    return out


# ------------------------------------------------------------------ requests


class Correction(BaseModel):
    field: str
    value: Any
    note: str = ""
    version: int | None = None


class Confirmation(BaseModel):
    field: str
    note: str = ""
    version: int | None = None


class Submission(BaseModel):
    justification: str
    vendor_verification: dict[str, Any] | None = None
    version: int | None = None


class Approval(BaseModel):
    note: str = ""
    version: int | None = None


class Rejection(BaseModel):
    reason: str = Field(min_length=1)
    version: int | None = None


# ------------------------------------------------------------------ routes


@app.get("/invoices")
def list_invoices(
    session: SessionDep,
    state: str | None = None,
    decision: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    stmt = select(InvoiceRow).order_by(InvoiceRow.seq)
    if state:
        stmt = stmt.where(InvoiceRow.state == state)
    if decision:
        stmt = stmt.where(InvoiceRow.decision == decision)
    rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
    return {"items": [_view(r) for r in rows]}


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, session: SessionDep) -> dict[str, Any]:
    row = session.get(InvoiceRow, invoice_id)
    if row is None:
        raise HTTPException(404, f"invoice {invoice_id} not found")
    events = session.execute(
        select(AuditEvent).where(AuditEvent.invoice_id == invoice_id).order_by(AuditEvent.seq)
    ).scalars()
    return _view(row, detail=True) | {
        "audit": [
            {
                "seq": e.seq,
                "at": e.at,
                "actor": e.actor,
                "action": e.action,
                "payload": e.payload,
                "hash": e.hash,
            }
            for e in events
        ]
    }


@app.get("/invoices/{invoice_id}/image")
def get_image(invoice_id: str, session: SessionDep) -> Response:
    row = session.get(InvoiceRow, invoice_id)
    if row is None:
        raise HTTPException(404, f"invoice {invoice_id} not found")
    return Response(render_first_page_png(Path(row.pdf_path), dpi=110), media_type="image/png")


@app.post("/invoices/{invoice_id}/corrections")
def post_correction(
    invoice_id: str, body: Correction, session: SessionDep, ctx: CtxDep, user: UserDep
) -> dict[str, Any]:
    row = _run(
        session,
        service.correct_field,
        ctx,
        user,
        invoice_id,
        body.field,
        body.value,
        body.note,
        body.version,
    )
    return _view(row, detail=True)


@app.post("/invoices/{invoice_id}/confirmations")
def post_confirmation(
    invoice_id: str, body: Confirmation, session: SessionDep, ctx: CtxDep, user: UserDep
) -> dict[str, Any]:
    row = _run(
        session, service.confirm_field, ctx, user, invoice_id, body.field, body.note, body.version
    )
    return _view(row, detail=True)


@app.post("/invoices/{invoice_id}/submit")
def post_submit(
    invoice_id: str, body: Submission, session: SessionDep, user: UserDep
) -> dict[str, Any]:
    row = _run(
        session,
        service.submit,
        user,
        invoice_id,
        body.justification,
        body.vendor_verification,
        body.version,
    )
    return _view(row, detail=True)


@app.post("/invoices/{invoice_id}/approve")
def post_approve(
    invoice_id: str, body: Approval, session: SessionDep, user: UserDep
) -> dict[str, Any]:
    row = _run(session, service.approve, user, invoice_id, body.note, body.version)
    return _view(row, detail=True)


@app.post("/invoices/{invoice_id}/reject")
def post_reject(
    invoice_id: str, body: Rejection, session: SessionDep, user: UserDep
) -> dict[str, Any]:
    row = _run(session, service.reject, user, invoice_id, body.reason, body.version)
    return _view(row, detail=True)


@app.get("/summary")
def get_summary(session: SessionDep) -> dict[str, Any]:
    return service.summary(session)


@app.get("/audit/verify")
def get_audit_verify(session: SessionDep) -> dict[str, Any]:
    return audit.verify_chain(session)
