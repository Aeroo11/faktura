"""Misread model: alignment, known misreads, and that it never opens a payment path."""

import pytest

from faktura.learn.misread import Correction, MisreadModel, align
from faktura.master import MasterData
from faktura.matching import Ledger, Status, decide
from faktura.schemas import Vendor


@pytest.mark.parametrize(
    ("printed", "read", "edits"),
    [
        ("1234", "1234", []),
        ("1264", "1204", ["6>0"]),
        ("1234", "2134", ["swap"]),
        ("1234", "12334", ["ins-dup"]),
        ("1234", "12534", ["ins"]),
        ("3664", "364", ["del-dup"]),
        ("1234", "124", ["del"]),
        ("1669", "1009", ["6>0", "6>0"]),
        ("1234", "9876", None),  # a different number, not a misread
    ],
)
def test_align(printed, read, edits):
    assert align(printed, read) == edits


def model(*edits: str, times: int = 3) -> MisreadModel:
    """A model that has seen each edit `times` times."""
    m = MisreadModel()
    for e in edits:
        a, b = e.split(">")
        for _ in range(times):
            m.add(Correction("vendor_npwp", f"1{b}2", f"1{a}2"))
    return m


def test_only_edits_seen_often_enough_are_known():
    m = model("6>0", times=3)
    m.add(Correction("bank_account", "172", "142"))  # 4>7 seen once
    assert m.known("6>0") and not m.known("4>7")
    assert m.explain("1004", "1604") is not None
    assert m.explain("1774", "1744") is None  # 4>7: seen, but not often enough
    assert m.explain("9999", "1604") is None


def test_more_than_two_edits_is_not_a_misread():
    m = model("6>0")
    assert m.explain("1000", "1600") is not None
    assert m.explain("1000", "1660") is not None  # two edits
    assert m.explain("0000", "6660") is None  # three


def test_non_identifier_fields_and_rewrites_are_ignored():
    m = MisreadModel()
    m.add(Correction("total", "100", "160"))
    m.add(Correction("vendor_npwp", "1234567890123456", "9999999999999999"))
    m.add(Correction("vendor_npwp", None, "1234"))
    assert m.n_corrections == 0 and m.n_skipped == 1


def test_save_load_roundtrip_keeps_version(tmp_path):
    m = model("6>0", "9>0")
    m.trained_on = {"events": 6}
    m.save(tmp_path / "m.json")
    again = MisreadModel.load(tmp_path / "m.json")
    assert again.version == m.version and again.edit_counts == m.edit_counts
    assert again.trained_on == {"events": 6}


# ------------------------------------------------------------------ decide()


VENDOR = Vendor(
    vendor_id="V001",
    name="PT Contoh",
    npwp="0123456789012346",
    address="Jl. Contoh",
    bank_name="BNI",
    bank_account="1234567896",
)


@pytest.fixture
def case():
    from faktura.extract.schema import ExtractedInvoice

    md = MasterData(vendors={"V001": VENDOR}, purchase_orders={}, goods_receipts={})
    ext = ExtractedInvoice.model_validate(
        {
            "invoice_number": "INV/1",
            "tax_invoice_number": "0" * 16,
            "invoice_date": "2025-07-01",
            "po_number": None,
            "vendor_name": "PT Contoh",
            "vendor_npwp": VENDOR.npwp,
            "bank_name": "BNI",
            "bank_account": VENDOR.bank_account,
            "lines": [],
            "dpp": 100,
            "vat": 11,
            "total": 111,
            "uncertain_fields": [],
        }
    )
    return md, ext


def codes(d):
    return {r.code for r in d.reasons}


def test_known_misread_of_bank_account_is_checked_first_not_held(case):
    md, ext = case
    misread = ext.model_copy(update={"bank_account": "1234567890"})  # last 6 read as 0
    plain = decide("i", misread, md, Ledger(), 1100)
    learned = decide("i", misread, md, Ledger(), 1100, misread=model("6>0"))
    assert plain.status is Status.HOLD and "bank_master" in codes(plain)
    assert "probable_misread" in codes(learned) and "bank_master" not in codes(learned)
    assert learned.status is not Status.APPROVE  # never paid on the model's word
    # The message must not reveal the master value.
    msg = next(r.message for r in learned.reasons if r.code == "probable_misread")
    assert VENDOR.bank_account not in msg and "6" not in msg


def test_once_a_person_confirms_the_page_the_hold_comes_back(case):
    md, ext = case
    forged = ext.model_copy(update={"bank_account": "1234567890"})  # printed like this
    d = decide("i", forged, md, Ledger(), 1100, misread=model("6>0"), verified={"bank_account"})
    assert d.status is Status.HOLD and "bank_master" in codes(d)


def test_unknown_difference_and_bank_name_stay_held(case):
    md, ext = case
    m = model("6>0")
    other = ext.model_copy(update={"bank_account": "9876543210"})
    assert decide("i", other, md, Ledger(), 1100, misread=m).status is Status.HOLD
    renamed = ext.model_copy(update={"bank_account": "1234567890", "bank_name": "Bank Lain"})
    d = decide("i", renamed, md, Ledger(), 1100, misread=m)
    assert d.status is Status.HOLD and "bank_name_master" in codes(d)


def test_hint_names_kinds_of_mistakes_never_values():
    m = model("6>0", "9>0")
    m.add(Correction("bank_account", "1223", "123"))
    m.add(Correction("bank_account", "4556", "456"))
    hint = m.hint()
    assert "a printed 6 read as 0 (3x)" in hint and "a digit read twice (2x)" in hint
    assert "1223" not in hint and "456" not in hint
    assert MisreadModel().hint() is None
