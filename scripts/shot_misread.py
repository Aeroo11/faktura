"""Screenshot an invoice that the learned misread model triaged (for docs/learning.md).

Needs Keycloak, the API (with FAKTURA_MISREAD_MODEL set) and the web app running.

    uv run python scripts/shot_misread.py --out docs/screenshots
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select

from faktura.app.db import InvoiceRow, make_engine, make_sessionmaker

WEB = "http://127.0.0.1:3077"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("docs/screenshots"))
    args = parser.parse_args()
    load_dotenv()
    with make_sessionmaker(make_engine())() as s:
        rows = s.execute(
            select(InvoiceRow)
            .where(InvoiceRow.state == "needs_action", InvoiceRow.decision == "review_reading")
            .order_by(InvoiceRow.seq)
        ).scalars()
        # One whose only problem is the triaged identifier, so the screenshot is clear.
        inv_id = next(r.id for r in rows if {x["code"] for x in r.reasons} == {"probable_misread"})
    print("invoice:", inv_id)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"{WEB}/login")
        page.get_by_role("link", name="Sign in with Keycloak").click()
        page.locator("#username").fill("budi")
        page.locator("#password").fill("budi-faktura-dev")
        page.locator("#kc-login").click()
        expect(page.get_by_test_id("signed-in-user")).to_be_visible(timeout=20_000)
        page.goto(f"{WEB}/invoices/{inv_id}")
        expect(page.get_by_text("Looks like a misread").first).to_be_visible()
        page.screenshot(path=args.out / "6-probable-misread.png", full_page=True)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
