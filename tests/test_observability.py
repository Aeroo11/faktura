"""Request metrics and logs, and the gauges read from the database."""

import json
import logging

from prometheus_client import CollectorRegistry, generate_latest

from faktura.app.api import app
from faktura.app.db import make_engine
from faktura.app.observability import REQUESTS, JsonFormatter, WorkCollector, trace_engine
from tests.helpers import clean, ingest


def _count(method, route, status):
    return REQUESTS.labels(method, route, status)._value.get()


def test_requests_are_counted_by_route_template_with_a_request_id(env, caplog):
    client, labels, _ = env
    inv = ingest(env, clean(labels)[0])
    before = _count("GET", "/invoices/{invoice_id}", "200")
    with caplog.at_level(logging.INFO, logger="faktura.request"):
        r = client.get(f"/invoices/{inv}", headers={"X-Request-ID": "demo-request-0001"})
    assert r.headers["x-request-id"] == "demo-request-0001"
    assert _count("GET", "/invoices/{invoice_id}", "200") == before + 1  # not the raw path
    record = next(r for r in caplog.records if r.name == "faktura.request")
    assert record.fields["user"] == "budi" and record.fields["status"] == 200
    line = json.loads(JsonFormatter().format(record))
    assert line["route"] == "/invoices/{invoice_id}" and line["request_id"] == "demo-request-0001"

    unmatched = _count("GET", "unmatched", "404")
    r = client.get("/no/such/path", headers={"X-Request-ID": "bad id with spaces"})
    assert r.headers["x-request-id"] != "bad id with spaces"  # replaced, not echoed
    assert _count("GET", "unmatched", "404") == unmatched + 1


def test_sql_spans_carry_the_statement_but_not_the_parameters(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from sqlalchemy import text

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    engine = make_engine("sqlite://")
    trace_engine(engine, tracer_provider=provider)
    with engine.connect() as conn:
        conn.execute(text("SELECT :account"), {"account": "1234567890"})
    [span] = exporter.get_finished_spans()
    assert span.name == "SELECT sqlite"
    assert span.attributes["db.query.text"] == "SELECT ?"
    assert "1234567890" not in str(dict(span.attributes))


def test_work_gauges_come_from_the_database(env):
    client, labels, _ = env
    ok = ingest(env, clean(labels)[0])
    ingest(env, clean(labels)[1], bank_account="9" * 10)  # held: fraud_hold
    registry = CollectorRegistry()
    registry.register(WorkCollector(app.state.sessions))
    assert "faktura_invoices" in generate_latest(registry).decode()  # exposition works

    def value(name, **labels):
        return registry.get_sample_value(name, labels)

    approved = {"state": "approved", "decision": "approve"}
    assert value("faktura_invoices", **approved) == 1
    assert value("faktura_invoices", state="needs_action", decision="fraud_hold") == 1
    total = client.get(f"/invoices/{ok}").json()["total"]
    assert value("faktura_invoice_amount_rupiah", **approved) == total
    assert value("faktura_erp_outbox", status="pending") == 1
    assert value("faktura_audit_chain_ok") == 1
    assert value("faktura_db_up") == 1
