"""Render an invoice to a PDF page in one of several layouts, then degrade it.

Degradation levels:
    0  clean digital PDF
    1  mild scan: small rotation, light blur and noise
    2  poor scan: low contrast, fold shadow, low resolution, JPEG artifacts
    3  phone photo: perspective skew, uneven lighting, low resolution, colour cast

Every page carries a "CONTOH SINTETIS" banner so it can never pass as a real document.
"""

from __future__ import annotations

import datetime as dt
import io
import random
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from faktura.schemas import Invoice
from faktura.synth.config import SynthConfig

PAGE_W, PAGE_H = 1240, 1754  # A4 at 150 dpi
DPI = 150
M = 80  # margin
RIGHT = PAGE_W - M
BUYER = ("PT Contoh Pembeli Sintetis", "Jl. Fiktif No. 1, Surabaya")
WATERMARK = "CONTOH SINTETIS - BUKAN DOKUMEN ASLI"

_MONTHS = {
    "id": [
        "Januari",
        "Februari",
        "Maret",
        "April",
        "Mei",
        "Juni",
        "Juli",
        "Agustus",
        "September",
        "Oktober",
        "November",
        "Desember",
    ],
    "en": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
}

LABELS = {
    "id": {
        "title": "FAKTUR",
        "no": "No. Faktur",
        "tax_no": "No. Faktur Pajak",
        "date": "Tanggal",
        "po": "No. PO",
        "to": "Kepada",
        "desc": "Uraian",
        "qty": "Jml",
        "price": "Harga Satuan",
        "amount": "Jumlah",
        "dpp": "DPP",
        "vat": "PPN",
        "total": "Total",
        "bank": "Pembayaran ke",
        "stamp": "DITERIMA",
    },
    "en": {
        "title": "INVOICE",
        "no": "Invoice No.",
        "tax_no": "Tax Invoice No.",
        "date": "Date",
        "po": "PO No.",
        "to": "Bill To",
        "desc": "Description",
        "qty": "Qty",
        "price": "Unit Price",
        "amount": "Amount",
        "dpp": "Tax Base",
        "vat": "VAT",
        "total": "Total",
        "bank": "Pay to",
        "stamp": "RECEIVED",
    },
}


# ---------------------------------------------------------------- formatting


def fmt_money(n: int, style: str) -> str:
    if style == "id_rp":
        return "Rp " + f"{n:,}".replace(",", ".")
    if style == "id_rp_dec":
        return "Rp" + f"{n:,}".replace(",", ".") + ",00"
    if style == "idr_comma":
        return f"IDR {n:,}"
    raise ValueError(style)


def fmt_date(d: dt.date, style: str, lang: str = "id") -> str:
    if style == "long":
        return f"{d.day} {_MONTHS[lang][d.month - 1]} {d.year}"
    if style == "iso":
        return d.isoformat()
    if style == "mon":
        return f"{d.day:02d}-{_MONTHS['en'][d.month - 1][:3]}-{d.year}"
    return d.strftime("%d/%m/%Y")  # Indonesian day-first, even on English layouts


def fmt_npwp(npwp: str, grouped: bool) -> str:
    return " ".join(npwp[i : i + 4] for i in range(0, 16, 4)) if grouped else npwp


@dataclass(frozen=True)
class RenderSpec:
    layout: str
    lang: str
    date_style: str
    money_style: str
    npwp_grouped: bool


# ---------------------------------------------------------------- drawing


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


class Page:
    def __init__(self) -> None:
        self.img = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        self.d = ImageDraw.Draw(self.img)

    def text(self, xy, s, size=22, fill="black", anchor="la", bold=False) -> None:
        # Bold is faked with a stroke, which closes the gap in "5" so it reads as "6".
        # Real invoices use a true bold face, so never stroke text that contains digits:
        # that error would come from this renderer, not from the documents being simulated.
        stroke = 1 if bold and not any(ch.isdigit() for ch in s) else 0
        self.d.text(
            xy, s, fill=fill, font=_font(size), anchor=anchor, stroke_width=stroke, stroke_fill=fill
        )

    def wrap(self, s: str, size: int, width: int) -> list[str]:
        words, lines, cur = s.split(), [], ""
        for w in words:
            cand = f"{cur} {w}".strip()
            if self.d.textlength(cand, font=_font(size)) <= width or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        return [*lines, cur] if cur else lines

    def watermark(self) -> None:
        self.text((M, 30), WATERMARK, 18, (170, 170, 170))
        self.text((M, PAGE_H - 50), WATERMARK, 18, (170, 170, 170))


@dataclass(frozen=True)
class Col:
    x: int
    align: str  # "l" or "r"
    wrap_width: int = 0


def draw_table(
    p: Page,
    cols: list[Col],
    header: list[str],
    rows: list[list[str]],
    y: int,
    size: int = 22,
    row_h: int = 32,
    rules: bool = True,
    zebra: tuple[int, int, int] | None = None,
    header_fill: tuple[int, int, int] | None = None,
) -> int:
    left, right = M, RIGHT
    hdr_color = "white" if header_fill else "black"
    if header_fill:
        p.d.rectangle((left, y - 6, right, y + row_h), fill=header_fill)
    elif rules:
        p.d.line((left, y - 8, right, y - 8), fill="black", width=2)
    for c, h in zip(cols, header, strict=True):
        p.text((c.x, y), h, size, hdr_color, anchor="la" if c.align == "l" else "ra", bold=True)
    y += row_h + 8
    if rules and not header_fill:
        p.d.line((left, y - 8, right, y - 8), fill="black", width=1)
    for i, row in enumerate(rows):
        cells = [
            p.wrap(v, size, c.wrap_width) if c.wrap_width else [v]
            for c, v in zip(cols, row, strict=True)
        ]
        height = max(len(c) for c in cells) * row_h
        if zebra and i % 2 == 1:
            p.d.rectangle((left, y - 4, right, y + height - 4), fill=zebra)
        for c, lines in zip(cols, cells, strict=True):
            for k, line in enumerate(lines):
                p.text((c.x, y + k * row_h), line, size, anchor="la" if c.align == "l" else "ra")
        y += height
    if rules:
        p.d.line((left, y + 4, right, y + 4), fill="black", width=2)
    return y + 20


def _rows(inv: Invoice, s: RenderSpec, order: list[str]) -> list[list[str]]:
    rows = []
    for i, ln in enumerate(inv.lines, start=1):
        cell = {
            "#": str(i),
            "desc": ln.description,
            "qty": str(ln.qty),
            "price": fmt_money(ln.unit_price, s.money_style),
            "amount": fmt_money(ln.amount, s.money_style),
        }
        rows.append([cell[k] for k in order])
    return rows


def _totals(inv: Invoice, lb: dict[str, str]) -> list[tuple[str, int]]:
    return [(lb["dpp"], inv.dpp), (lb["vat"], inv.vat), (lb["total"], inv.total)]


Layout = Callable[[Page, Invoice, RenderSpec], tuple[int, int]]


def layout_classic(p: Page, inv: Invoice, s: RenderSpec) -> tuple[int, int]:
    lb = LABELS[s.lang]
    p.text((M, 90), inv.vendor_name, 26)
    p.text((M, 126), f"NPWP: {fmt_npwp(inv.vendor_npwp, s.npwp_grouped)}")
    p.text((RIGHT, 90), lb["title"], 44, anchor="ra")
    meta = [
        (lb["no"], inv.invoice_number),
        (lb["tax_no"], inv.tax_invoice_number),
        (lb["date"], fmt_date(inv.invoice_date, s.date_style, s.lang)),
        (lb["po"], inv.po_number),
    ]
    for i, (k, v) in enumerate(meta):
        p.text((M, 220 + i * 32), k)
        p.text((M + 230, 220 + i * 32), f": {v}")
    p.text((PAGE_W // 2 + 60, 220), f"{lb['to']}:")
    p.text((PAGE_W // 2 + 60, 252), BUYER[0])
    p.text((PAGE_W // 2 + 60, 284), BUYER[1])

    cols = [Col(M, "l"), Col(M + 60, "l", 470), Col(700, "r"), Col(920, "r"), Col(RIGHT, "r")]
    y = draw_table(
        p,
        cols,
        ["#", lb["desc"], lb["qty"], lb["price"], lb["amount"]],
        _rows(inv, s, ["#", "desc", "qty", "price", "amount"]),
        400,
    )
    totals_xy = (700, y)
    for k, v in _totals(inv, lb):
        big = k == lb["total"]
        p.text((760, y), k, 26 if big else 22, bold=big)
        p.text((RIGHT, y), fmt_money(v, s.money_style), 26 if big else 22, anchor="ra", bold=big)
        y += 38
    p.text((M, y + 40), f"{lb['bank']}: {inv.bank_name} {inv.bank_account}")
    p.text((M, y + 72), f"a.n. {inv.vendor_name}")
    return totals_xy


def layout_modern(p: Page, inv: Invoice, s: RenderSpec) -> tuple[int, int]:
    lb = LABELS[s.lang]
    navy = (35, 55, 85)
    p.d.rectangle((0, 60, PAGE_W, 200), fill=navy)
    p.text((M, 95), lb["title"], 52, "white", bold=True)
    p.text((RIGHT, 92), inv.vendor_name, 26, "white", anchor="ra")
    p.text(
        (RIGHT, 132), f"NPWP {fmt_npwp(inv.vendor_npwp, s.npwp_grouped)}", 20, "white", anchor="ra"
    )
    grid = [
        (M, 240, lb["no"], inv.invoice_number),
        (M, 310, lb["tax_no"], inv.tax_invoice_number),
        (640, 240, lb["date"], fmt_date(inv.invoice_date, s.date_style, s.lang)),
        (640, 310, lb["po"], inv.po_number),
    ]
    for x, y, k, v in grid:
        p.text((x, y), k.upper(), 16, (110, 110, 110))
        p.text((x, y + 24), v, 24)
    p.text((M, 390), lb["to"].upper(), 16, (110, 110, 110))
    p.text((M, 414), f"{BUYER[0]}, {BUYER[1]}", 22)

    cols = [Col(M + 10, "l", 520), Col(740, "r"), Col(950, "r"), Col(RIGHT - 10, "r")]
    y = draw_table(
        p,
        cols,
        [lb["desc"], lb["qty"], lb["price"], lb["amount"]],
        _rows(inv, s, ["desc", "qty", "price", "amount"]),
        490,
        rules=False,
        zebra=(236, 239, 244),
        header_fill=navy,
    )
    box_top = y + 10
    p.d.rectangle((680, box_top, RIGHT, box_top + 150), outline=navy, width=2)
    yy = box_top + 18
    for k, v in _totals(inv, lb):
        big = k == lb["total"]
        p.text((700, yy), k, 24 if big else 22, bold=big)
        p.text(
            (RIGHT - 20, yy), fmt_money(v, s.money_style), 24 if big else 22, anchor="ra", bold=big
        )
        yy += 42
    p.text((M, box_top + 200), lb["bank"].upper(), 16, (110, 110, 110))
    p.text((M, box_top + 224), f"{inv.bank_name} - {inv.bank_account} a.n. {inv.vendor_name}")
    return (680, box_top)


def layout_compact(p: Page, inv: Invoice, s: RenderSpec) -> tuple[int, int]:
    lb = LABELS[s.lang]
    body = 18
    p.text((M, 70), f"{inv.vendor_name}  |  NPWP {fmt_npwp(inv.vendor_npwp, s.npwp_grouped)}", 22)
    p.text((RIGHT, 70), f"{lb['bank']}: {inv.bank_name} {inv.bank_account}", body, anchor="ra")
    p.text((M, 115), lb["title"], 32, bold=True)
    date = fmt_date(inv.invoice_date, s.date_style, s.lang)
    p.text(
        (M, 170),
        f"{lb['no']} {inv.invoice_number}    {lb['date']} {date}    {lb['po']} {inv.po_number}",
        body,
    )
    p.text((M, 200), f"{lb['to']}: {BUYER[0]}, {BUYER[1]}", body)

    cols = [Col(M, "l", 560), Col(830, "r"), Col(910, "r"), Col(RIGHT, "r")]
    y = draw_table(
        p,
        cols,
        [lb["desc"], lb["price"], lb["qty"], lb["amount"]],
        _rows(inv, s, ["desc", "price", "qty", "amount"]),
        260,
        size=body,
        row_h=26,
    )
    totals_xy = (760, y)
    for k, v in _totals(inv, lb):
        p.text((900, y), k, body, anchor="ra", bold=k == lb["total"])
        p.text((RIGHT, y), fmt_money(v, s.money_style), body, anchor="ra", bold=k == lb["total"])
        y += 28
    p.text((M, PAGE_H - 110), f"{lb['tax_no']}: {inv.tax_invoice_number}", body)
    return totals_xy


def layout_borderless(p: Page, inv: Invoice, s: RenderSpec) -> tuple[int, int]:
    lb = LABELS[s.lang]
    p.text((M, 90), lb["title"].title(), 40)
    p.text((RIGHT, 90), inv.vendor_name, 24, anchor="ra", bold=True)
    p.text((RIGHT, 124), f"NPWP {fmt_npwp(inv.vendor_npwp, s.npwp_grouped)}", 20, anchor="ra")
    meta = [
        (lb["no"], inv.invoice_number),
        (lb["date"], fmt_date(inv.invoice_date, s.date_style, s.lang)),
        (lb["po"], inv.po_number),
        (lb["tax_no"], inv.tax_invoice_number),
    ]
    for i, (k, v) in enumerate(meta):
        p.text((M, 200 + i * 30), f"{k}  {v}", 20)
    p.text((700, 200), lb["to"], 20, (110, 110, 110))
    p.text((700, 230), BUYER[0], 20)
    p.text((700, 260), BUYER[1], 20)

    cols = [Col(150, "r"), Col(190, "l", 500), Col(900, "r"), Col(RIGHT, "r")]
    y = draw_table(
        p,
        cols,
        [lb["qty"], lb["desc"], lb["price"], lb["amount"]],
        _rows(inv, s, ["qty", "desc", "price", "amount"]),
        380,
        size=20,
        row_h=30,
        rules=False,
    )
    y += 20
    totals_xy = (M, y)
    for k, v in _totals(inv, lb):  # totals on the left, unlike the other layouts
        p.text((M, y), k, 20, bold=k == lb["total"])
        p.text((M + 380, y), fmt_money(v, s.money_style), 20, anchor="ra", bold=k == lb["total"])
        y += 32
    p.text((RIGHT, y + 60), f"{inv.bank_name} {inv.bank_account}", 20, anchor="ra")
    p.text((RIGHT, y + 90), inv.vendor_name, 20, anchor="ra")
    return totals_xy


LAYOUTS: dict[str, Layout] = {
    "classic": layout_classic,
    "modern": layout_modern,
    "compact": layout_compact,
    "borderless": layout_borderless,
}


def draw_invoice(inv: Invoice, spec: RenderSpec) -> tuple[Image.Image, tuple[int, int]]:
    p = Page()
    p.watermark()
    totals_xy = LAYOUTS[spec.layout](p, inv, spec)
    return p.img, totals_xy


# ---------------------------------------------------------------- degradation


def _noise(img: Image.Image, rng: random.Random, sigma: float, alpha: float) -> Image.Image:
    noise = Image.effect_noise(img.size, sigma)
    if img.mode == "RGB":
        noise = noise.convert("RGB")
    return Image.blend(img, noise, alpha)


def _jpeg(img: Image.Image, quality: int) -> Image.Image:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert(img.mode)


def _downscale(img: Image.Image, factor: float) -> Image.Image:
    small = img.resize((int(img.width * factor), int(img.height * factor)), Image.Resampling.BOX)
    return small.resize(img.size, Image.Resampling.BILINEAR)


def _multiply(img: Image.Image, mask_l: Image.Image) -> Image.Image:
    mask = mask_l.convert(img.mode)
    return ImageChops.multiply(img, mask)


def _fold(img: Image.Image, rng: random.Random) -> Image.Image:
    mask = Image.new("L", img.size, 255)
    d = ImageDraw.Draw(mask)
    if rng.random() < 0.5:
        x = rng.randint(img.width // 4, 3 * img.width // 4)
        d.rectangle((x - 12, 0, x + 12, img.height), fill=150)
    else:
        y = rng.randint(img.height // 4, 3 * img.height // 4)
        d.rectangle((0, y - 12, img.width, y + 12), fill=150)
    return _multiply(img, mask.filter(ImageFilter.GaussianBlur(22)))


def _lighting(img: Image.Image, rng: random.Random, darkest: int) -> Image.Image:
    grad = Image.linear_gradient("L").resize(img.size).rotate(rng.uniform(0, 360), fillcolor=255)
    grad = grad.point(lambda v: darkest + v * (255 - darkest) // 255)
    return _multiply(img, grad)


def _perspective(img: Image.Image, rng: random.Random) -> Image.Image:
    w, h = img.size

    def j(scale: int) -> int:
        return rng.randint(-int(scale * 0.06), int(scale * 0.06))

    # Source quad corners: upper-left, lower-left, lower-right, upper-right.
    quad = (j(w), j(h), j(w), h + j(h), w + j(w), h + j(h), w + j(w), j(h))
    return img.transform(
        img.size,
        Image.Transform.QUAD,
        quad,
        resample=Image.Resampling.BICUBIC,
        fillcolor=(95, 85, 75),
    )


def _stamp(img: Image.Image, xy: tuple[int, int], text: str, date: str, rng: random.Random):
    layer = Image.new("RGBA", (440, 170), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    red = (190, 30, 40, 165)
    d.rounded_rectangle((6, 6, 434, 164), radius=18, outline=red, width=7)
    d.text((220, 62), text, fill=red, font=_font(56), anchor="mm", stroke_width=1, stroke_fill=red)
    d.text((220, 122), date, fill=red, font=_font(30), anchor="mm")
    layer = layer.rotate(rng.uniform(-20, 20), expand=True, resample=Image.Resampling.BICUBIC)
    out = img.convert("RGBA")
    out.alpha_composite(layer, (max(0, xy[0] + rng.randint(-40, 80)), max(0, xy[1] - 30)))
    return out.convert("RGB")


def degrade(img: Image.Image, level: int, rng: random.Random) -> tuple[Image.Image, float]:
    """Returns the degraded page and its rotation in degrees."""
    angle = 0.0
    if level == 0:
        return img, angle
    if level == 1:
        angle = rng.uniform(-1.5, 1.5)
        out = img.convert("L").rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
        out = out.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 0.9)))
        return _noise(out, rng, rng.uniform(10, 30), rng.uniform(0.04, 0.10)), angle
    if level == 2:
        out = img.convert("L")
        out = ImageEnhance.Contrast(out).enhance(rng.uniform(0.5, 0.7))
        out = ImageEnhance.Brightness(out).enhance(rng.uniform(1.0, 1.15))
        out = _fold(out, rng)
        out = _downscale(out, rng.uniform(0.5, 0.65))
        angle = rng.uniform(-3, 3)
        out = out.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=235)
        out = _noise(out, rng, rng.uniform(20, 40), rng.uniform(0.06, 0.12))
        return _jpeg(out, rng.randint(30, 45)), angle
    # level 3: phone photo
    out = _perspective(img, rng)
    out = _lighting(out, rng, darkest=rng.randint(110, 160))
    tint = Image.new("RGB", out.size, (255, rng.randint(225, 245), rng.randint(190, 215)))
    out = ImageChops.multiply(out, tint)
    out = _downscale(out, rng.uniform(0.4, 0.55))
    out = out.filter(ImageFilter.GaussianBlur(rng.uniform(0.8, 1.4)))
    return _jpeg(out, rng.randint(25, 40)), angle


# ---------------------------------------------------------------- entry point


def choose_spec(rng: random.Random, cfg: SynthConfig) -> RenderSpec:
    rc = cfg.render
    names = list(rc.layout_weights)
    layout = rng.choices(names, weights=[rc.layout_weights[n] for n in names])[0]
    return RenderSpec(
        layout=layout,
        lang=rng.choice(["id", "en"]),
        date_style=rng.choice(rc.date_styles),
        money_style=rng.choice(rc.money_styles),
        npwp_grouped=rng.random() < 0.5,
    )


def choose_level(rng: random.Random, cfg: SynthConfig) -> int:
    lw = cfg.render.level_weights
    if lw is None:
        return 1 if rng.random() < cfg.scan_effect_rate else 0
    levels = sorted(lw)
    return rng.choices(levels, weights=[lw[k] for k in levels])[0]


def render_invoice(
    inv: Invoice, path: Path, rng: random.Random, cfg: SynthConfig
) -> dict[str, str | int | float | bool]:
    spec = choose_spec(rng, cfg)
    img, totals_xy = draw_invoice(inv, spec)
    level = choose_level(rng, cfg)
    stamped = level >= 2 and rng.random() < cfg.render.stamp_rate
    if stamped:
        stamp_date = fmt_date(inv.invoice_date + dt.timedelta(days=rng.randint(1, 10)), "short")
        img = _stamp(img, totals_xy, LABELS[spec.lang]["stamp"], stamp_date, rng)
    img, angle = degrade(img, level, rng)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PDF", resolution=DPI)
    return {
        "layout": spec.layout,
        "lang": spec.lang,
        "date_style": spec.date_style,
        "money_style": spec.money_style,
        "npwp_grouped": spec.npwp_grouped,
        "level": level,
        "scanned": level > 0,
        "stamp": stamped,
        "rotation_deg": round(angle, 2),
    }
