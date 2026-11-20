"""Vendor bank-account changes: request, call-back to the number on file, third-person approval."""

import dataclasses
from collections import Counter

from faktura.app.api import app
from faktura.master import StaticMaster
from tests.helpers import APPROVER, APPROVER2, STAFF, STAFF2, clean, ingest

NEW_ACCOUNT = "7712345678"
NEW_BANK = "Bank Contoh Baru"


def _vendor(lb):
    md = app.state.ctx.master.snapshot()
    return next(v for v in md.vendors.values() if v.name == lb.invoice.vendor_name)


def _same_vendor(labels, n):
    """n clean invoices from one vendor, in arrival order."""
    counts = Counter(lb.invoice.vendor_name for lb in clean(labels))
    name = next(name for name, c in counts.most_common() if c >= n)
    return [lb for lb in clean(labels) if lb.invoice.vendor_name == name][:n]


def _held_for_new_account(env, lb, **kw):
    client, _, _ = env
    inv = ingest(env, lb, bank_account=NEW_ACCOUNT, bank_name=NEW_BANK, **kw)
    body = client.get(f"/invoices/{inv}").json()
    assert body["decision"] == "fraud_hold"
    assert {"bank_master", "bank_name_master"} <= set(body["reason_codes"])
    return inv


def _check_page(client, inv):
    for field in ("bank_account", "bank_name"):
        r = client.post(f"/invoices/{inv}/confirmations", headers=STAFF, json={"field": field})
        assert r.status_code == 200, r.text


def _request(client, inv):
    r = client.post(
        "/bank-changes",
        headers=STAFF,
        json={"source_invoice_id": inv, "channel": "printed on the invoice, with a cover letter"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_a_new_account_is_never_accepted_on_the_invoice(env):
    client, labels, _ = env
    inv = _held_for_new_account(env, clean(labels)[0])
    r = client.post(
        f"/invoices/{inv}/submit",
        headers=STAFF,
        json={
            "justification": "vendor emailed us their new account",
            "vendor_verification": {
                "contact_name": "Pak Andi",
                "contact_channel": "phone number on the letter",
                "confirmed_details": "new account",
            },
        },
    )
    assert r.status_code == 409 and "bank-change request" in r.json()["detail"]


def test_request_needs_the_page_checked_first(env):
    client, labels, _ = env
    inv = _held_for_new_account(env, clean(labels)[0])
    r = client.post("/bank-changes", json={"source_invoice_id": inv, "channel": "on invoice"})
    assert r.status_code == 409 and "against the page" in r.json()["detail"]


def test_vendor_denies_on_callback_so_the_invoice_is_rejected(env):
    """The BEC case: the vendor, called on the number on file, never changed banks."""
    client, labels, _ = env
    lb = clean(labels)[0]
    inv = _held_for_new_account(env, lb)
    _check_page(client, inv)
    change = _request(client, inv)
    assert change["status"] == "awaiting_callback"
    assert change["old"]["bank_account"] == _vendor(lb).bank_account
    assert change["new"] == {"bank_name": NEW_BANK, "bank_account": NEW_ACCOUNT}

    cb = {"spoke_with": "Ibu Rina, finance", "outcome": "denied", "note": "no change made"}
    url = f"/bank-changes/{change['id']}/callback"
    assert client.post(url, headers=STAFF, json=cb).status_code == 403  # the requester
    assert client.post(url, headers=APPROVER, json=cb).status_code == 403  # wrong role
    r = client.post(url, headers=STAFF2, json=cb)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    assert r.json()["callback"]["phone"] == _vendor(lb).phone  # from the master, not typed

    body = client.get(f"/invoices/{inv}").json()
    assert body["state"] == "rejected"
    assert "likely payment fraud" in body["audit"][-2]["payload"]["reason"]
    assert body["bank_change"]["status"] == "rejected"


def test_confirmed_change_needs_a_third_person_then_releases_and_replaces(env):
    client, labels, _ = env
    first, second, third = _same_vendor(labels, 3)
    inv = _held_for_new_account(env, first)
    _check_page(client, inv)
    change = _request(client, inv)
    cid = change["id"]
    r = client.post(
        f"/bank-changes/{cid}/callback",
        headers=STAFF2,
        json={"spoke_with": "Pak Budi Santoso, director", "outcome": "confirmed"},
    )
    assert r.json()["status"] == "awaiting_approval"
    # The invoice is still held until the change is approved.
    assert client.get(f"/invoices/{inv}").json()["decision"] == "fraud_hold"

    r = client.post(f"/bank-changes/{cid}/approve", headers=APPROVER, json={"note": "call ok"})
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text

    body = client.get(f"/invoices/{inv}").json()
    assert body["decision"] == "approve" and body["state"] == "awaiting_approval"
    # Whoever approved the master change cannot also release the payment.
    r = client.post(f"/invoices/{inv}/approve", headers=APPROVER, json={})
    assert r.status_code == 403
    r = client.post(f"/invoices/{inv}/approve", headers=APPROVER2, json={})
    assert r.json()["state"] == "approved"

    # From now on the new account is the vendor's account, and the old one is not.
    later = ingest(env, second, bank_account=NEW_ACCOUNT, bank_name=NEW_BANK)
    assert client.get(f"/invoices/{later}").json()["state"] == "approved"
    old = ingest(env, third)
    assert "bank_master" in client.get(f"/invoices/{old}").json()["reason_codes"]


def test_approver_must_not_be_involved_and_rejection_keeps_the_hold(env):
    client, labels, _ = env
    inv = _held_for_new_account(env, clean(labels)[0])
    _check_page(client, inv)
    cid = _request(client, inv)["id"]
    client.post(
        f"/bank-changes/{cid}/callback",
        headers=STAFF2,
        json={"spoke_with": "someone", "outcome": "confirmed"},
    )
    r = client.post(f"/bank-changes/{cid}/approve", headers=STAFF, json={})
    assert r.status_code == 403
    r = client.post(f"/bank-changes/{cid}/reject", headers=APPROVER, json={"note": ""})
    assert r.status_code == 422
    r = client.post(
        f"/bank-changes/{cid}/reject", headers=APPROVER, json={"note": "letterhead looks forged"}
    )
    assert r.json()["status"] == "rejected"
    assert client.get(f"/invoices/{inv}").json()["decision"] == "fraud_hold"


def test_one_open_request_per_vendor_and_manual_requests(env):
    client, labels, _ = env
    lb = clean(labels)[0]
    vendor = _vendor(lb)
    body = {
        "vendor_id": vendor.vendor_id,
        "new_bank_name": NEW_BANK,
        "new_bank_account": "7712-3456-78",
        "channel": "letter from the vendor, received by post",
    }
    r = client.post("/bank-changes", json=body)
    assert r.status_code == 200 and r.json()["new"]["bank_account"] == NEW_ACCOUNT
    assert client.post("/bank-changes", json=body).status_code == 409
    assert client.get("/bank-changes?status=awaiting_callback").json()["items"][0]["id"] == 1
    detail = client.get("/bank-changes/1").json()
    assert detail["vendor"]["phone_on_file"] == vendor.phone
    same = body | {"new_bank_account": vendor.bank_account, "new_bank_name": vendor.bank_name}
    assert client.post("/bank-changes", json=same).status_code in (409, 422)
    assert client.get("/bank-changes/99").status_code == 404


def test_no_phone_on_file_blocks_the_callback(env):
    client, labels, _ = env
    lb = clean(labels)[0]
    vendor = _vendor(lb)
    md = app.state.ctx.master.snapshot()
    vendors = dict(md.vendors) | {vendor.vendor_id: vendor.model_copy(update={"phone": ""})}
    app.state.ctx.master = StaticMaster(dataclasses.replace(md, vendors=vendors))
    r = client.post(
        "/bank-changes",
        json={
            "vendor_id": vendor.vendor_id,
            "new_bank_name": NEW_BANK,
            "new_bank_account": NEW_ACCOUNT,
            "channel": "email from the vendor",
        },
    )
    r = client.post(
        f"/bank-changes/{r.json()['id']}/callback",
        headers=STAFF2,
        json={"spoke_with": "the number in the email", "outcome": "confirmed"},
    )
    assert r.status_code == 409 and "no phone number on file" in r.json()["detail"]
