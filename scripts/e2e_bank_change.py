"""End-to-end demo of a vendor bank-account change in a real browser, with screenshots.

Two invoices are held because they ask to be paid into a bank account that is not on
file. Nothing on an invoice tells a fraudulent change from a real one, so both go
through the same bank-change request:

* **Fraud (business email compromise).** Budi checks the account against the page and
  records the request; Dewi calls the vendor on the number on file; the vendor never
  changed banks. The request and the invoice are rejected.
* **Real change.** Same steps, but the vendor confirms. Sari, who took no part, approves
  the change; the invoice is decided again and now matches, and Rina (not Sari, who
  changed the master) approves the payment. With --erp, faktura-erp-sync trusts the new
  account in Odoo and posts the bill into it.

Needs Keycloak, the API and the web app (see README); reads the database directly
(DATABASE_URL) to pick the invoices and check the outcome.

    uv run python scripts/e2e_bank_change.py --out docs/screenshots --erp
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from e2e_demo import WEB, goto, sign_in
from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select
from sqlalchemy.orm import Session

from faktura.app import audit
from faktura.app.db import AuditEvent, BankChangeRow, InvoiceRow, make_engine, make_sessionmaker
from faktura.evals.extraction import load_labels


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def pick_held_for_new_account(session: Session, data_dir: Path, n: int = 2) -> list[str]:
    """Invoices held only for a new bank account, read correctly, from different vendors."""
    labels = {lb.invoice_id: lb for lb in load_labels(data_dir)}
    busy = set(
        session.execute(
            select(BankChangeRow.vendor_id).where(
                BankChangeRow.status.in_(["awaiting_callback", "awaiting_approval"])
            )
        ).scalars()
    )
    picked: list[str] = []
    rows = session.execute(
        select(InvoiceRow).where(InvoiceRow.state == "needs_action").order_by(InvoiceRow.seq)
    ).scalars()
    for row in rows:
        lb = labels[row.id]
        if {r["code"] for r in row.reasons} != {"bank_master"} or row.vendor_id in busy:
            continue
        if _digits(row.reading.get("bank_account")) != _digits(lb.invoice.bank_account):
            continue  # a misread; this demo is about real changes
        picked.append(row.id)
        busy.add(row.vendor_id)
        if len(picked) == n:
            return picked
    raise SystemExit("not enough invoices held for a new bank account; reseed the database")


def request_from_invoice(page: Page, inv_id: str, out: Path | None) -> int:
    goto(page, f"{WEB}/invoices/{inv_id}")
    expect(page.get_by_text("Bank account differs from vendor master").first).to_be_visible()
    expect(page.get_by_role("button", name="Submit for approval")).to_have_count(0)
    if out:
        page.screenshot(path=out / "7-held-new-bank-account.png", full_page=True)
    # Budi compares the account with the page: it really is printed like this.
    row = page.locator("li").filter(has=page.get_by_text("Bank account", exact=True))
    row.get_by_role("button", name="Reading is correct").click()
    expect(page.get_by_text("Field confirmed").first).to_be_visible(timeout=20_000)
    goto(page, f"{WEB}/invoices/{inv_id}")
    page.get_by_placeholder("How did the change arrive?").fill(
        "printed on the invoice, with a cover letter announcing a new bank"
    )
    page.get_by_role("button", name="Open bank-change request").click()
    page.wait_for_url(re.compile(r"/bank-changes/\d+$"), timeout=20_000)
    page.wait_for_load_state("networkidle")
    expect(page.get_by_text("Awaiting call-back").first).to_be_visible()
    expect(page.get_by_text("a colleague must make the call-back")).to_be_visible()
    return int(page.url.rsplit("/", 1)[1])


def call_back(page: Page, change_id: int, outcome: str, out: Path | None) -> None:
    goto(page, f"{WEB}/bank-changes/{change_id}")
    expect(page.get_by_text("Call the number on file in the vendor master")).to_be_visible()
    page.get_by_placeholder("Who did you speak to?").fill("Ibu Wulan, finance manager")
    label = "Vendor confirmed this account" if outcome == "confirmed" else "Vendor did not change"
    page.get_by_label(label).check()
    page.get_by_placeholder("Note (optional)").fill(
        "asked her to read the new account number back"
        if outcome == "confirmed"
        else "no change of bank; they have not sent any letter"
    )
    if out:
        page.screenshot(path=out / "8-callback-number-on-file.png", full_page=True)
    page.get_by_role("button", name="Record call-back").click()
    # The form is replaced by the recorded outcome once the page refreshes.
    done = "Vendor confirmed" if outcome == "confirmed" else "Vendor denied the change"
    expect(page.get_by_text(done, exact=True).first).to_be_visible(timeout=20_000)


def run_scenario(page: Page, out: Path, fraud_inv: str, real_inv: str, erp: bool) -> None:
    # 1. Fraud: the vendor never changed banks.
    sign_in(page, "budi")
    fraud = request_from_invoice(page, fraud_inv, out)
    sign_in(page, "dewi")
    call_back(page, fraud, "denied", out)
    goto(page, f"{WEB}/invoices/{fraud_inv}")
    expect(page.get_by_text("Rejected by").first).to_be_visible()
    expect(page.get_by_text("likely payment fraud").first).to_be_visible()
    page.screenshot(path=out / "9-vendor-denied-invoice-rejected.png", full_page=True)

    # 2. A real change, confirmed on the call-back and approved by a third person.
    sign_in(page, "budi")
    real = request_from_invoice(page, real_inv, None)
    sign_in(page, "dewi")
    call_back(page, real, "confirmed", None)
    sign_in(page, "sari")
    goto(page, f"{WEB}/bank-changes/{real}")
    page.get_by_role("button", name="Approve change").click()
    expect(page.get_by_text("Approved. Faktura now pays").first).to_be_visible(timeout=20_000)
    goto(page, f"{WEB}/invoices/{real_inv}")
    expect(page.get_by_text("Awaiting approval").first).to_be_visible()
    # Sari changed the master, so she cannot also release this payment.
    expect(page.get_by_role("button", name="Approve payment")).to_have_count(0)
    sign_in(page, "rina")
    goto(page, f"{WEB}/invoices/{real_inv}")
    page.get_by_role("button", name="Approve payment").click()
    expect(page.get_by_text("Approved by").first).to_be_visible(timeout=20_000)

    if erp:
        subprocess.run(["uv", "run", "faktura-erp-sync", "--once"], check=True)
        goto(page, f"{WEB}/invoices/{real_inv}")
        expect(page.get_by_text("Posted to Odoo as vendor bill").first).to_be_visible()
    goto(page, f"{WEB}/bank-changes/{real}")
    if erp:
        expect(page.get_by_text("New account trusted in Odoo").first).to_be_visible()
    page.screenshot(path=out / "10-bank-change-approved.png", full_page=True)
    goto(page, f"{WEB}/bank-changes")
    page.screenshot(path=out / "11-bank-changes.png")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data/synth/v2"))
    parser.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    parser.add_argument("--erp", action="store_true", help="also write to Odoo (docs/erp.md)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    load_dotenv()
    sessions = make_sessionmaker(make_engine())
    with sessions() as s:
        fraud_inv, real_inv = pick_held_for_new_account(s, args.data)
    print(f"fraud invoice: {fraud_inv} | real change: {real_inv}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            run_scenario(page, args.out, fraud_inv, real_inv, args.erp)
        except Exception:
            page.screenshot(path=args.out / "failure.png", full_page=True)
            raise
        finally:
            browser.close()

    with sessions() as s:
        fraud, real = (s.get(InvoiceRow, i) for i in (fraud_inv, real_inv))
        changes = {
            c.source_invoice_id: c
            for c in s.execute(
                select(BankChangeRow).where(
                    BankChangeRow.source_invoice_id.in_([fraud_inv, real_inv])
                )
            ).scalars()
        }
        actions = [
            (e.actor, e.action)
            for e in s.execute(
                select(AuditEvent).where(AuditEvent.invoice_id == real_inv).order_by(AuditEvent.seq)
            ).scalars()
        ]
        chain = audit.verify_chain(s)
        print("fraud:", fraud.state, changes[fraud_inv].status, changes[fraud_inv].callback)
        print(
            "real:", real.state, real.makers, changes[real_inv].status, changes[real_inv].erp_status
        )
        print("audit (real):", actions)
        print("chain:", chain)
        assert fraud.state == "rejected" and changes[fraud_inv].status == "rejected"
        assert real.state == "approved" and changes[real_inv].status == "approved"
        assert ("rina", "approved") in actions and ("sari", "bank_change_approved") in actions
        if args.erp:
            assert changes[real_inv].erp_status == "synced"
            assert actions[-1] == ("erp-sync", "erp_bill_posted")
        assert chain["ok"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
