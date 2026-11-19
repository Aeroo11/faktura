import datetime as dt
import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from faktura.schemas import InvoiceLabel
from faktura.synth.config import SynthConfig, load_config
from faktura.synth.generate import CATALOG, LONG_DESCRIPTIONS, generate
from faktura.synth.render import (
    LAYOUTS,
    RenderSpec,
    degrade,
    draw_invoice,
    fmt_date,
    fmt_money,
    render_invoice,
)

V2 = Path(__file__).parent.parent / "configs" / "synth_v2.yaml"


@pytest.fixture(scope="module")
def invoice():
    cfg = SynthConfig(seed=4, n_purchase_orders=5, long_description_rate=1.0)
    return max((lb.invoice for lb in generate(cfg).labels), key=lambda i: len(i.lines))


@pytest.mark.parametrize(
    ("style", "expected"),
    [("id_rp", "Rp 1.234.567"), ("id_rp_dec", "Rp1.234.567,00"), ("idr_comma", "IDR 1,234,567")],
)
def test_fmt_money(style, expected):
    assert fmt_money(1_234_567, style) == expected


@pytest.mark.parametrize(
    ("style", "lang", "expected"),
    [
        ("short", "en", "05/03/2025"),  # always day-first
        ("long", "id", "5 Maret 2025"),
        ("long", "en", "5 March 2025"),
        ("iso", "id", "2025-03-05"),
        ("mon", "id", "05-Mar-2025"),
    ],
)
def test_fmt_date(style, lang, expected):
    assert fmt_date(dt.date(2025, 3, 5), style, lang) == expected


@pytest.mark.parametrize("layout", list(LAYOUTS))
@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_every_layout_and_level_renders(invoice, layout, level):
    spec = RenderSpec(layout, "id", "short", "id_rp_dec", True)
    img, (tx, ty) = draw_invoice(invoice, spec)
    assert 0 <= tx < img.width and 0 <= ty < img.height
    out, _ = degrade(img, level, random.Random(level))
    assert out.size == img.size


def test_long_descriptions_used_when_enabled(invoice):
    assert any(ln.description in LONG_DESCRIPTIONS.values() for ln in invoice.lines)


def test_long_descriptions_off_by_default():
    ds = generate(SynthConfig(seed=4, n_purchase_orders=30))
    short = {desc for _, desc, _, _ in CATALOG}
    assert all(ln.description in short for lb in ds.labels for ln in lb.invoice.lines)


def test_unknown_layout_rejected():
    with pytest.raises(ValidationError):
        SynthConfig.model_validate({"render": {"layout_weights": {"fancy": 1}}})


def test_v2_config_loads_and_labels_stay_correct():
    from tests.test_synth import violated_rules

    cfg = load_config(V2)
    ds = generate(cfg.model_copy(update={"n_purchase_orders": 150}))
    oracle = violated_rules(ds, cfg.vat_bps)
    assert all(set(lb.anomalies) == oracle[lb.invoice_id] for lb in ds.labels)


def test_level_survives_json_round_trip_as_int(invoice, tmp_path):
    cfg = load_config(V2)
    for seed in range(8):
        meta = render_invoice(invoice, tmp_path / f"{seed}.pdf", random.Random(seed), cfg)
        lb = InvoiceLabel(invoice_id="x", invoice=invoice, anomalies=[], render=meta)
        back = InvoiceLabel.model_validate_json(lb.model_dump_json())
        assert type(back.render["level"]) is int
        assert back.render["level"] == meta["level"]
