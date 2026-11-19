import io
import random

import numpy as np
import pytest
from PIL import Image

from faktura.extract.preprocess import LIGHTING_RATIO_MIN, preprocess
from faktura.synth.config import SynthConfig
from faktura.synth.generate import generate
from faktura.synth.render import RenderSpec, degrade, draw_invoice


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def invoice():
    return generate(SynthConfig(seed=6, n_purchase_orders=3, anomaly_rate=0)).labels[0].invoice


@pytest.mark.parametrize("layout", ["classic", "modern"])
@pytest.mark.parametrize("level", [0, 1, 2])
def test_non_photos_are_returned_unchanged(invoice, layout, level):
    img, _ = draw_invoice(invoice, RenderSpec(layout, "id", "short", "id_rp", True))
    png = _png(degrade(img, level, random.Random(level))[0])
    out, meta = preprocess(png)
    assert not meta["photo"]
    assert out == png


@pytest.mark.parametrize("seed", range(4))
def test_phone_photos_are_detected_and_evened(invoice, seed):
    img, _ = draw_invoice(invoice, RenderSpec("modern", "id", "short", "id_rp", True))
    png = _png(degrade(img, 3, random.Random(seed))[0])
    out, meta = preprocess(png)
    assert meta["photo"] and meta["lighting_ratio"] >= LIGHTING_RATIO_MIN
    assert not meta["warped"]  # warp is off by default: it cropped real content
    before = np.asarray(Image.open(io.BytesIO(png)).convert("L"), dtype=float)
    after = np.asarray(Image.open(io.BytesIO(out)).convert("L"), dtype=float)
    assert after.shape == before.shape  # nothing cropped

    # Paper brightness should vary less across the page after correction.
    def spread(a):
        h, w = a.shape
        q = [
            np.percentile(a[r : r + h // 3, c : c + w // 2], 90)
            for r in (0, h // 3, 2 * h // 3)
            for c in (0, w // 2)
        ]
        return max(q) - min(q)

    assert spread(after) < spread(before)
