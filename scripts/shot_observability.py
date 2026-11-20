"""Screenshots of the Grafana dashboard and of one request's trace in Jaeger.

Needs `docker compose --profile obs up -d` and an API started with FAKTURA_METRICS_PORT
and OTEL_EXPORTER_OTLP_ENDPOINT set, after some traffic (e.g. scripts/e2e_demo.py).

    uv run python scripts/shot_observability.py --out docs/screenshots
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

GRAFANA = "http://127.0.0.1:3300"
JAEGER = "http://127.0.0.1:16686"
OPERATION = "POST /invoices/{invoice_id}/corrections"


def latest_trace(operation: str) -> str:
    r = httpx.get(
        f"{JAEGER}/api/traces",
        params={"service": "faktura-api", "operation": operation, "limit": 1, "lookback": "6h"},
    )
    r.raise_for_status()
    traces = r.json()["data"]
    if not traces:
        raise SystemExit(f"no trace for {operation!r}; run scripts/e2e_demo.py first")
    return traces[0]["traceID"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    args = parser.parse_args()
    trace_id = latest_trace(OPERATION)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1250})
        page.goto(f"{GRAFANA}/d/faktura/faktura?orgId=1&from=now-1h&to=now&kiosk")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)  # panels render after their queries return
        page.screenshot(path=args.out / "obs-1-grafana.png")
        page.goto(f"{JAEGER}/trace/{trace_id}")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        page.screenshot(path=args.out / "obs-2-trace.png")
        browser.close()
    print("trace", trace_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
