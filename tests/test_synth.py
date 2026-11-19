from pathlib import Path

import pytest

from faktura.schemas import AnomalyType, InvoiceLabel, compute_vat
from faktura.synth.cli import main as synth_main
from faktura.synth.config import SynthConfig, load_config
from faktura.synth.generate import Dataset, generate

CONFIG = Path(__file__).parent.parent / "configs" / "synth.yaml"


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate(SynthConfig(seed=7, n_purchase_orders=400, anomaly_rate=0.5))


def violated_rules(ds: Dataset, vat_bps: int) -> dict[str, set[AnomalyType]]:
    """Independent oracle: which rules does each invoice actually break?"""
    vendors = {v.vendor_id: v for v in ds.vendors}
    pos = {p.po_number: p for p in ds.purchase_orders}
    grs = {g.po_number: g for g in ds.goods_receipts}
    seen: set[tuple[str, str]] = set()
    out: dict[str, set[AnomalyType]] = {}

    for lb in ds.labels:
        inv = lb.invoice
        po = pos[inv.po_number]
        vendor = vendors[po.vendor_id]
        price = {ln.sku: ln.unit_price for ln in po.lines}
        received = {ln.sku: ln.qty_received for ln in grs[inv.po_number].lines}
        broken: set[AnomalyType] = set()
        if any(ln.unit_price != price[ln.sku] for ln in inv.lines):
            broken.add(AnomalyType.PRICE_MISMATCH)
        if any(ln.qty > received[ln.sku] for ln in inv.lines):
            broken.add(AnomalyType.QTY_OVER_RECEIVED)
        if inv.vendor_npwp != vendor.npwp:
            broken.add(AnomalyType.NPWP_MISMATCH)
        if inv.vat != compute_vat(inv.dpp, vat_bps):
            broken.add(AnomalyType.VAT_MISCALC)
        if inv.bank_account != vendor.bank_account:
            broken.add(AnomalyType.BANK_ACCOUNT_CHANGED)
        key = (po.vendor_id, inv.invoice_number)
        if key in seen:
            broken.add(AnomalyType.DUPLICATE_INVOICE)
        seen.add(key)
        out[lb.invoice_id] = broken
    return out


def test_labels_match_independent_oracle(ds):
    oracle = violated_rules(ds, vat_bps=1100)
    wrong = {
        lb.invoice_id: (sorted(lb.anomalies), sorted(oracle[lb.invoice_id]))
        for lb in ds.labels
        if set(lb.anomalies) != oracle[lb.invoice_id]
    }
    assert not wrong


def test_every_anomaly_type_is_generated(ds):
    produced = {a for lb in ds.labels for a in lb.anomalies}
    assert produced == set(AnomalyType)


def test_printed_arithmetic_is_consistent(ds):
    for lb in ds.labels:
        inv = lb.invoice
        assert all(ln.amount == ln.qty * ln.unit_price for ln in inv.lines)
        assert inv.dpp == sum(ln.amount for ln in inv.lines)
        assert inv.total == inv.dpp + inv.vat


def test_duplicates_point_to_an_earlier_clean_invoice(ds):
    by_id = {lb.invoice_id: lb for lb in ds.labels}
    order = [lb.invoice_id for lb in ds.labels]
    for lb in ds.labels:
        if AnomalyType.DUPLICATE_INVOICE in lb.anomalies:
            orig = by_id[lb.duplicate_of]
            assert order.index(orig.invoice_id) < order.index(lb.invoice_id)
            assert not orig.anomalies
            assert orig.invoice.model_dump(exclude={"invoice_id"}) == lb.invoice.model_dump(
                exclude={"invoice_id"}
            )


def test_generation_is_deterministic():
    cfg = SynthConfig(seed=123, n_purchase_orders=50)
    a, b = generate(cfg), generate(cfg)
    assert [lb.model_dump() for lb in a.labels] == [lb.model_dump() for lb in b.labels]


def test_different_seeds_differ():
    a = generate(SynthConfig(seed=1, n_purchase_orders=20))
    b = generate(SynthConfig(seed=2, n_purchase_orders=20))
    assert [lb.invoice for lb in a.labels] != [lb.invoice for lb in b.labels]


def test_zero_anomaly_rate_is_all_clean():
    ds = generate(SynthConfig(seed=3, n_purchase_orders=100, anomaly_rate=0.0))
    assert all(not lb.anomalies for lb in ds.labels)


def test_repo_config_loads():
    cfg = load_config(CONFIG)
    assert cfg.vat_bps == 1100


def test_cli_writes_dataset_with_pdfs(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("seed: 5\nn_purchase_orders: 6\nscan_effect_rate: 0.5\n", encoding="utf-8")
    out = tmp_path / "ds"
    assert synth_main(["--config", str(cfg_path), "--out", str(out)]) == 0

    labels = [
        InvoiceLabel.model_validate_json(line)
        for line in (out / "labels.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert labels
    for lb in labels:
        pdf = out / "invoices" / f"{lb.invoice_id}.pdf"
        assert pdf.read_bytes()[:5] == b"%PDF-"
        assert {"lang", "scanned"} <= lb.render.keys()
    for name in ["vendors.json", "purchase_orders.json", "goods_receipts.json", "config.yaml"]:
        assert (out / name).exists()


def test_rendering_does_not_change_data(tmp_path):
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("seed: 9\nn_purchase_orders: 5\n", encoding="utf-8")
    synth_main(["--config", str(cfg_path), "--out", str(tmp_path / "a")])
    synth_main(["--config", str(cfg_path), "--out", str(tmp_path / "b"), "--no-render"])

    def invoices(p):
        return [
            InvoiceLabel.model_validate_json(x).invoice
            for x in (p / "labels.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    assert invoices(tmp_path / "a") == invoices(tmp_path / "b")
