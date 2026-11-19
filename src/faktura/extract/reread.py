"""Targeted second read of the fields the validators flagged.

The first pass reads the whole invoice once. The validators then say which fields cannot
be trusted. This module asks the model to read only those fields again, from a higher
resolution render, with more reasoning effort.

Design rule: the model is never shown master data. For FORMAT and INCONSISTENT issues the
reason is given ("NPWP was read with 17 digits; NPWP has 16"), since it contains nothing
but the document itself. For MASTER issues no reason is given: telling the model that a
value "differs from our records" would push it to find a different reading, and a
genuinely fraudulent NPWP that was read correctly could then be "corrected" away.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image
from pydantic import BaseModel, create_model

from faktura.extract.client import AzureResponsesClient, Usage
from faktura.extract.schema import ExtractedInvoice, FieldName
from faktura.validate import Issue, IssueKind

REREAD_PROMPT = """\
You are double-checking specific fields of an Indonesian supplier invoice. An automatic
check flagged them as possibly misread. Read ONLY the requested fields, directly from the
image, character by character.
Rules:
- Copy values as printed; do not correct, recompute, or infer anything.
- Money: integer rupiah. "Rp 1.234.567" -> 1234567.
- NPWP is a 16-digit number: count the digits. Bank account: digits only.
- In every identifier, check each character: 0 vs O, 1 vs I vs l, 5 vs 6 vs 8, and digits
  that may be doubled or skipped.
- Date: YYYY-MM-DD.
- For line items, list every line in printed order.
- Return null only if the field is not visible at all."""


def fields_to_reread(issues: list[Issue]) -> set[FieldName]:
    return {f for i in issues for f in i.fields}


def reread_hints(issues: list[Issue]) -> list[str]:
    """Reasons that may be shown to the model: document-only facts, never master data."""
    return sorted({i.message for i in issues if i.kind is not IssueKind.MASTER})


def subset_model(fields: set[FieldName]) -> type[BaseModel]:
    """A structured-output model containing only the requested ExtractedInvoice fields."""
    src = ExtractedInvoice.model_fields
    defs: dict[str, Any] = {
        f.value: (src[f.value].annotation, src[f.value]) for f in sorted(fields, key=str)
    }
    return create_model("ReRead", **defs)


def merge(ext: ExtractedInvoice, reread: BaseModel) -> ExtractedInvoice:
    """Take the second reading where it produced a value; keep the first otherwise."""
    update = {}
    for name, value in reread.model_dump().items():
        if value is None or (name == FieldName.LINES.value and not value):
            continue
        update[name] = value
    data = ext.model_dump() | update
    return ExtractedInvoice.model_validate(data)


def make_tiles(
    image_png: bytes, cols: int = 2, rows: int = 3, overlap: float = 0.12
) -> list[bytes]:
    """Overlapping crops of the page, so small text is seen at a higher effective resolution.

    Vision models downscale a full page before reading it; a crop of one sixth of the page
    keeps roughly twice as many pixels per character.
    """
    img = Image.open(io.BytesIO(image_png))
    w, h = img.size
    tw, th = w / cols, h / rows
    ox, oy = tw * overlap, th * overlap
    tiles = []
    for r in range(rows):
        for c in range(cols):
            box = (
                int(max(0, c * tw - ox)),
                int(max(0, r * th - oy)),
                int(min(w, (c + 1) * tw + ox)),
                int(min(h, (r + 1) * th + oy)),
            )
            buf = io.BytesIO()
            img.crop(box).save(buf, format="PNG")
            tiles.append(buf.getvalue())
    return tiles


def reread(
    client: AzureResponsesClient,
    image_png: bytes,
    ext: ExtractedInvoice,
    issues: list[Issue],
    reasoning: str | None = "medium",
    tiles: bool = False,
) -> tuple[ExtractedInvoice, set[FieldName], Usage, float]:
    fields = fields_to_reread(issues)
    if not fields:
        return ext, set(), Usage(), 0.0
    names = ", ".join(sorted(f.value for f in fields))
    text = f"Read these fields: {names}."
    hints = reread_hints(issues)
    if hints:
        text += " Checks that failed on the first reading: " + "; ".join(hints) + "."
    images = [(image_png, "image/png")]
    if tiles:
        text += (
            " The first image is the full page; the following images are zoomed,"
            " overlapping crops of it (left-to-right, top-to-bottom). Use the crops to"
            " read small characters."
        )
        images += [(t, "image/png") for t in make_tiles(image_png)]
    parsed, usage, latency = client.parse_images(
        images, REREAD_PROMPT, text, subset_model(fields), detail="high", reasoning=reasoning
    )
    return merge(ext, parsed), fields, usage, latency
