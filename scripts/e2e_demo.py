"""End-to-end demo of the review queue in a real browser, with screenshots.

Needs the API on :8077 and the web app on :3077 (see README). The scenario:
a clean invoice was held because its NPWP was misread; AP staff (Budi) corrects the
NPWP from the page, the engine re-checks it, and an approver (Sari) approves payment.

    uv run python scripts/e2e_demo.py --out docs/screenshots
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

from faktura.evals.extraction import load_labels

API = "http://127.0.0.1:8077"
WEB = "http://127.0.0.1:3077"


def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}{path}") as r:
        return json.load(r)


def pick_misread_npwp_hold(data_dir: Path) -> tuple[str, str]:
    """A clean invoice held only because its NPWP was misread; returns (id, true NPWP)."""
    labels = {lb.invoice_id: lb for lb in load_labels(data_dir)}
    items = api_get("/invoices?state=needs_action&decision=fraud_hold&limit=500")["items"]
    for inv in items:
        lb = labels[inv["id"]]
        if not lb.anomalies and inv["reason_codes"] == ["npwp_master"]:
            return inv["id"], lb.invoice.vendor_npwp
    raise SystemExit("no suitable invoice in the queue; reseed the database")


def goto(page: Page, url: str) -> None:
    # Wait until hydration and any in-flight refresh are done before interacting.
    page.goto(url)
    page.wait_for_load_state("networkidle")


def switch_user(page: Page, name: str) -> None:
    if page.locator("#user").input_value() == name:
        return
    # The select changes instantly in the browser, but the cookie is set by a server
    # action that runs asynchronously. Wait for that POST to finish, or the next page
    # load can still render as the previous user.
    with page.expect_response(lambda r: r.request.method == "POST" and r.ok):
        page.select_option("#user", name)
    page.wait_for_load_state("networkidle")


def run_scenario(page: Page, out: Path, inv_id: str, true_npwp: str) -> None:
    goto(page, WEB)
    switch_user(page, "budi")
    expect(page.get_by_text("Needs action").first).to_be_visible()
    page.screenshot(path=out / "1-queue.png")

    goto(page, f"{WEB}/invoices/{inv_id}")
    expect(page.get_by_text("NPWP differs from vendor master").first).to_be_visible()
    page.screenshot(path=out / "2-held-for-npwp.png", full_page=True)

    # Budi checks the page and corrects the misread NPWP.
    row = page.locator("li").filter(has=page.get_by_text("NPWP", exact=True))
    row.get_by_role("button", name="Correct", exact=True).click()
    row.locator("input[name=value]").fill(true_npwp)
    row.locator("input[name=note]").fill("read from the printed invoice")
    row.get_by_role("button", name="Save correction").click()
    expect(page.get_by_text("Correction saved").first).to_be_visible(timeout=20_000)
    goto(page, f"{WEB}/invoices/{inv_id}")
    expect(page.get_by_text("Awaiting approval").first).to_be_visible()
    expect(page.get_by_role("button", name="Approve payment")).to_have_count(0)  # not an approver
    page.screenshot(path=out / "3-corrected-awaiting-approval.png", full_page=True)

    # Sari, a different person with the approver role, approves.
    switch_user(page, "sari")
    goto(page, f"{WEB}/invoices/{inv_id}")
    page.get_by_role("button", name="Approve payment").click()
    # The closed invoice says who approved it (from the audit trail), not a passing toast.
    expect(page.get_by_text("Approved by").first).to_be_visible(timeout=20_000)
    expect(page.get_by_text("sari", exact=True).first).to_be_visible()
    goto(page, f"{WEB}/invoices/{inv_id}")
    expect(page.get_by_text("Hash chain intact").first).to_be_visible()
    expect(page.get_by_text("Field corrected").first).to_be_visible()
    page.screenshot(path=out / "4-approved-with-audit.png", full_page=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data/synth/v2"))
    parser.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    inv_id, true_npwp = pick_misread_npwp_hold(args.data)
    print(f"scenario invoice: {inv_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            run_scenario(page, args.out, inv_id, true_npwp)
        except Exception:
            page.screenshot(path=args.out / "failure.png", full_page=True)
            raise
        finally:
            browser.close()

    detail = api_get(f"/invoices/{inv_id}")
    actions = [e["action"] for e in detail["audit"]]
    print("final state:", detail["state"], "| makers:", detail["makers"])
    print("audit actions:", actions)
    print("chain:", api_get("/audit/verify"))
    assert detail["state"] == "approved" and detail["makers"] == ["budi"]
    assert "field_corrected" in actions and actions[-2:] == ["approved", "state_changed"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
