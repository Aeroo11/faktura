"""End-to-end demo of the review queue in a real browser, with screenshots.

Needs Keycloak on :8180, the API on :8077 and the web app on :3077 (see README). Every
user signs in through the Keycloak login form. The scenario: a clean invoice was held
because its NPWP was misread; AP staff (Budi) corrects the NPWP from the page, the engine
re-checks it, and an approver (Sari) approves payment. An account holding both roles
(Joko) is refused at sign-in.

The script reads the database directly (DATABASE_URL) to pick the invoice and check the
outcome, since the API only answers signed-in users.

    uv run python scripts/e2e_demo.py --out docs/screenshots
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select
from sqlalchemy.orm import Session

from faktura.app import audit
from faktura.app.db import AuditEvent, InvoiceRow, make_engine, make_sessionmaker
from faktura.evals.extraction import load_labels

WEB = "http://127.0.0.1:3077"


def password(user: str) -> str:
    return f"{user}-faktura-dev"  # local development realm, keycloak/faktura-realm.json


def pick_misread_npwp_hold(session: Session, data_dir: Path) -> tuple[str, str]:
    """A clean invoice held only because its NPWP was misread; returns (id, true NPWP)."""
    labels = {lb.invoice_id: lb for lb in load_labels(data_dir)}
    rows = session.execute(
        select(InvoiceRow)
        .where(InvoiceRow.state == "needs_action", InvoiceRow.decision == "fraud_hold")
        .order_by(InvoiceRow.seq)
    ).scalars()
    for row in rows:
        lb = labels[row.id]
        if not lb.anomalies and sorted({r["code"] for r in row.reasons}) == ["npwp_master"]:
            return row.id, lb.invoice.vendor_npwp
    raise SystemExit("no suitable invoice in the queue; reseed the database")


def goto(page: Page, url: str) -> None:
    # Wait until hydration and any in-flight refresh are done before interacting.
    page.goto(url)
    page.wait_for_load_state("networkidle")


def sign_out(page: Page) -> None:
    button = page.get_by_role("button", name="Sign out")
    if button.count():
        button.click()
        expect(page.get_by_text("Signed out.")).to_be_visible()


def keycloak_login(page: Page, user: str, out: Path | None = None) -> None:
    """From Faktura's sign-in page, through the Keycloak form and back."""
    goto(page, f"{WEB}/login")
    if out:
        page.screenshot(path=out / "0-sign-in.png")
    page.get_by_role("link", name="Sign in with Keycloak").click()
    page.locator("#username").fill(user)
    page.locator("#password").fill(password(user))
    if out:
        page.screenshot(path=out / "0-keycloak-login.png")
    page.locator("#kc-login").click()


def sign_in(page: Page, user: str, out: Path | None = None) -> None:
    sign_out(page)
    keycloak_login(page, user, out)
    expect(page.get_by_test_id("signed-in-user")).to_be_visible(timeout=20_000)
    page.wait_for_load_state("networkidle")


def dual_role_is_refused(page: Page) -> None:
    sign_out(page)
    keycloak_login(page, "joko")
    expect(page.get_by_text("holds both AP staff and Approver")).to_be_visible(timeout=20_000)
    goto(page, WEB)  # and no session was created
    expect(page.get_by_role("link", name="Sign in with Keycloak")).to_be_visible()


def run_scenario(page: Page, out: Path, inv_id: str, true_npwp: str, erp: bool) -> None:
    dual_role_is_refused(page)
    sign_in(page, "budi", out)
    goto(page, WEB)
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
    sign_in(page, "sari")
    goto(page, f"{WEB}/invoices/{inv_id}")
    page.get_by_role("button", name="Approve payment").click()
    # The closed invoice says who approved it (from the audit trail), not a passing toast.
    expect(page.get_by_text("Approved by").first).to_be_visible(timeout=20_000)
    expect(page.get_by_text("sari", exact=True).first).to_be_visible()
    goto(page, f"{WEB}/invoices/{inv_id}")
    expect(page.get_by_text("Hash chain intact").first).to_be_visible()
    expect(page.get_by_text("Field corrected").first).to_be_visible()
    page.screenshot(path=out / "4-approved-with-audit.png", full_page=True)

    if erp:
        # The ERP sync worker posts the approved invoice to Odoo as a vendor bill.
        subprocess.run(["uv", "run", "faktura-erp-sync", "--once"], check=True)
        goto(page, f"{WEB}/invoices/{inv_id}")
        expect(page.get_by_text("Posted to Odoo as vendor bill").first).to_be_visible()
        page.screenshot(path=out / "5-posted-to-odoo.png", full_page=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data/synth/v2"))
    parser.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    parser.add_argument(
        "--erp", action="store_true", help="also post the bill to Odoo (needs docs/erp.md setup)"
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    load_dotenv()
    sessions = make_sessionmaker(make_engine())
    with sessions() as s:
        inv_id, true_npwp = pick_misread_npwp_hold(s, args.data)
    print(f"scenario invoice: {inv_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            run_scenario(page, args.out, inv_id, true_npwp, args.erp)
        except Exception:
            page.screenshot(path=args.out / "failure.png", full_page=True)
            raise
        finally:
            browser.close()

    with sessions() as s:
        row = s.get(InvoiceRow, inv_id)
        events = s.execute(
            select(AuditEvent).where(AuditEvent.invoice_id == inv_id).order_by(AuditEvent.seq)
        ).scalars()
        actions = [(e.actor, e.action) for e in events]
        chain = audit.verify_chain(s)
        state, makers = row.state, row.makers
    print("final state:", state, "| makers:", makers)
    print("audit:", actions)
    print("chain:", chain)
    assert state == "approved" and makers == ["budi"]
    assert ("budi", "field_corrected") in actions and ("sari", "approved") in actions
    actions = [a for _, a in actions]
    tail = ["approved", "state_changed", "erp_queued"] + (["erp_bill_posted"] if args.erp else [])
    assert actions[-len(tail) :] == tail
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
