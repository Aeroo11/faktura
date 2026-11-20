"""Metrics, request logs, and traces. See docs/observability.md.

* **Metrics** (Prometheus): request rate, errors and latency per route, plus the state of
  the work itself, read from the database at scrape time: invoices and rupiah per state
  and decision, ERP outbox backlog and its oldest pending item, bank changes waiting, and
  whether the audit hash chain still verifies. Served on a separate port bound to
  127.0.0.1 (FAKTURA_METRICS_PORT), so the API's sign-in rules stay unchanged and the
  numbers are not public.
* **Request logs**: one JSON line per request with a request id (returned as
  X-Request-ID), route, status, duration, user, and the trace id when tracing is on.
* **Traces** (OpenTelemetry, OTLP): on when OTEL_EXPORTER_OTLP_ENDPOINT is set. Spans
  for each request, every SQL statement, every call to Odoo, and the matching decision.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from prometheus_client import REGISTRY, Counter, Histogram, start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from faktura.app import audit
from faktura.app.db import BankChangeRow, ErpOutbox, InvoiceRow, OutboxStatus

log = logging.getLogger("faktura.request")

REQUESTS = Counter("faktura_http_requests", "HTTP requests handled", ["method", "route", "status"])
LATENCY = Histogram(
    "faktura_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


# ------------------------------------------------------------------ requests


class RequestMetrics:
    """ASGI middleware: metrics and a JSON log line per HTTP request.

    The route label is the path template ("/invoices/{invoice_id}"), never the raw path,
    so the number of series stays bounded; unmatched paths share one label.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        status = 500
        t0 = time.perf_counter()

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = time.perf_counter() - t0
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope["method"]
            REQUESTS.labels(method, route, str(status)).inc()
            LATENCY.labels(method, route).observe(elapsed)
            user = scope["state"].get("user")
            log.info(
                "request",
                extra={
                    "fields": {
                        "request_id": request_id,
                        "method": method,
                        "route": route,
                        "path": scope["path"],
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 1),
                        "user": getattr(user, "name", None),
                        "trace_id": _trace_id(),
                    }
                },
            )


def _trace_id() -> str | None:
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
    except ImportError:  # pragma: no cover
        return None
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out |= getattr(record, "fields", {})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def configure_logging(level: str | None = None) -> None:
    """JSON lines on stderr for Faktura's loggers (uvicorn keeps its own format)."""
    logger = logging.getLogger("faktura")
    if any(isinstance(h.formatter, JsonFormatter) for h in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level or os.environ.get("FAKTURA_LOG_LEVEL", "INFO"))
    logger.propagate = False


# ------------------------------------------------------------------ the work itself


class WorkCollector(Collector):
    """Gauges read from the database when Prometheus scrapes, cached for `ttl_s`.

    Gauges, not counters: the database is the record of what is waiting, so the numbers
    are right after a restart and agree with what the review queue shows.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        ttl_s: float = 10.0,
        chain_ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._sessions = sessions
        self._ttl = ttl_s
        self._chain_ttl = chain_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, list[GaugeMetricFamily]] | None = None
        self._chain: tuple[float, dict[str, Any]] | None = None

    def collect(self) -> Iterable[GaugeMetricFamily]:
        with self._lock:
            now = self._clock()
            if self._cached is None or now - self._cached[0] >= self._ttl:
                self._cached = (now, self._read(now))
            return list(self._cached[1])

    def _read(self, now: float) -> list[GaugeMetricFamily]:
        up = GaugeMetricFamily("faktura_db_up", "Whether the last read of the database worked")
        try:
            with self._sessions() as s:
                families = self._families(s, now)
        except Exception:
            logging.getLogger("faktura.metrics").exception("metrics read failed")
            up.add_metric([], 0)
            return [up]
        up.add_metric([], 1)
        return [up, *families]

    def _families(self, s: Session, now: float) -> list[GaugeMetricFamily]:
        invoices = GaugeMetricFamily(
            "faktura_invoices",
            "Invoices by workflow state and engine decision",
            labels=["state", "decision"],
        )
        amount = GaugeMetricFamily(
            "faktura_invoice_amount_rupiah",
            "Invoice totals by workflow state and engine decision",
            labels=["state", "decision"],
        )
        for state, decision, n, total in s.execute(
            select(
                InvoiceRow.state, InvoiceRow.decision, func.count(), func.sum(InvoiceRow.total)
            ).group_by(InvoiceRow.state, InvoiceRow.decision)
        ):
            invoices.add_metric([state, decision], n)
            amount.add_metric([state, decision], int(total or 0))

        outbox = GaugeMetricFamily(
            "faktura_erp_outbox", "Approved invoices by ERP posting status", labels=["status"]
        )
        counts = dict(
            s.execute(select(ErpOutbox.status, func.count()).group_by(ErpOutbox.status)).all()
        )
        for status in OutboxStatus:
            outbox.add_metric([status.value], counts.get(status.value, 0))
        oldest = s.execute(
            select(func.min(ErpOutbox.created_at)).where(ErpOutbox.status == OutboxStatus.PENDING)
        ).scalar()
        age = GaugeMetricFamily(
            "faktura_erp_outbox_oldest_pending_seconds",
            "Age of the oldest approved invoice not yet posted to the ERP (0 if none)",
        )
        age.add_metric([], _age_s(oldest))

        changes = GaugeMetricFamily(
            "faktura_bank_changes",
            "Vendor bank-account change requests by status",
            labels=["status"],
        )
        for status, n in s.execute(
            select(BankChangeRow.status, func.count()).group_by(BankChangeRow.status)
        ):
            changes.add_metric([status], n)
        changes_erp = GaugeMetricFamily(
            "faktura_bank_changes_erp",
            "Approved bank changes by ERP write status",
            labels=["erp_status"],
        )
        for status, n in s.execute(
            select(BankChangeRow.erp_status, func.count())
            .where(BankChangeRow.erp_status.is_not(None))
            .group_by(BankChangeRow.erp_status)
        ):
            changes_erp.add_metric([status], n)

        # Verifying the chain reads every event; done at most once a minute.
        if self._chain is None or now - self._chain[0] >= self._chain_ttl:
            self._chain = (now, audit.verify_chain(s))
        chain = self._chain[1]
        chain_ok = GaugeMetricFamily(
            "faktura_audit_chain_ok", "1 if the audit hash chain verifies, else 0"
        )
        chain_ok.add_metric([], 1 if chain["ok"] else 0)
        events = GaugeMetricFamily("faktura_audit_events", "Audit events checked")
        events.add_metric([], chain["events"])
        return [invoices, amount, outbox, age, changes, changes_erp, chain_ok, events]


def _age_s(iso: str | None) -> float:
    if not iso:
        return 0.0
    then = dt.datetime.fromisoformat(iso)
    return max(0.0, (dt.datetime.now(dt.UTC) - then).total_seconds())


_metrics_started = False


def start_metrics_server(sessions: sessionmaker[Session]) -> int | None:
    """Serve /metrics on FAKTURA_METRICS_PORT (off when unset). Once per process."""
    global _metrics_started
    port = os.environ.get("FAKTURA_METRICS_PORT")
    if not port or _metrics_started:
        return None
    REGISTRY.register(WorkCollector(sessions))
    start_http_server(int(port), addr=os.environ.get("FAKTURA_METRICS_ADDR", "127.0.0.1"))
    _metrics_started = True
    return int(port)


# ------------------------------------------------------------------ traces


def tracing_enabled() -> bool:
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def init_tracing(service_name: str, app=None) -> bool:
    """Export traces over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set.

    Call before the app serves its first request: middleware cannot be added later.
    """
    if not tracing_enabled():
        return False
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)  # calls to Odoo
    return True


def trace_engine(engine: Engine, tracer_provider=None) -> None:
    """A span per SQL statement, when tracing is on.

    Hand-rolled on SQLAlchemy's cursor events: the contrib SQLAlchemy instrumentor
    (0.66b2) refuses SQLAlchemy 2.1. Statement text only, never the parameters, which
    hold bank accounts and amounts.
    """
    if not tracing_enabled():
        return
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from sqlalchemy import event

    tracer = trace.get_tracer("faktura.sql", tracer_provider=tracer_provider)
    system = engine.dialect.name

    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn, cursor, statement, parameters, context, executemany):
        op = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "SQL"
        span = tracer.start_span(f"{op} {system}", kind=SpanKind.CLIENT)
        span.set_attribute("db.system.name", system)
        span.set_attribute("db.query.text", statement[:2000])
        conn.info.setdefault("_otel_spans", []).append(span)

    @event.listens_for(engine, "after_cursor_execute")
    def _end(conn, cursor, statement, parameters, context, executemany):
        spans = conn.info.get("_otel_spans")
        if spans:
            spans.pop().end()

    @event.listens_for(engine, "handle_error")
    def _error(ctx):
        spans = ctx.connection.info.get("_otel_spans") if ctx.connection is not None else None
        if spans:
            span = spans.pop()
            span.set_status(Status(StatusCode.ERROR, type(ctx.original_exception).__name__))
            span.end()
