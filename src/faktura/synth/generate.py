"""Deterministic generation of vendors, POs, goods receipts and labelled invoices."""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass

from faktura.schemas import (
    AnomalyType,
    GoodsReceipt,
    GRLine,
    Invoice,
    InvoiceLabel,
    InvoiceLine,
    POLine,
    PurchaseOrder,
    Vendor,
)
from faktura.synth.anomalies import INJECTORS, recompute_totals
from faktura.synth.config import SynthConfig

# (sku, description, min_price, max_price) in rupiah.
CATALOG: list[tuple[str, str, int, int]] = [
    ("ATK-001", "Kertas HVS A4 80gsm (rim)", 45_000, 65_000),
    ("ATK-002", "Tinta printer hitam", 85_000, 250_000),
    ("ATK-003", "Map plastik kancing", 3_000, 8_000),
    ("ATK-004", "Pulpen gel 0.5mm (lusin)", 30_000, 60_000),
    ("IT-001", "Mouse wireless", 75_000, 350_000),
    ("IT-002", "Keyboard USB", 90_000, 450_000),
    ("IT-003", "Kabel LAN Cat6 (meter)", 4_000, 9_000),
    ("IT-004", "SSD 512GB", 550_000, 1_200_000),
    ("SFT-001", "Helm proyek", 45_000, 120_000),
    ("SFT-002", "Sepatu safety", 250_000, 750_000),
    ("SFT-003", "Rompi reflektif", 35_000, 90_000),
    ("CLN-001", "Cairan pembersih lantai 5L", 60_000, 140_000),
    ("CLN-002", "Tisu gulung (pak isi 10)", 35_000, 70_000),
    ("LOG-001", "Palet kayu", 90_000, 180_000),
    ("LOG-002", "Stretch film 50cm", 70_000, 130_000),
]

LONG_DESCRIPTIONS: dict[str, str] = {
    "ATK-001": "Kertas HVS A4 80gsm isi 500 lembar per rim, putih, untuk printer laser dan inkjet",
    "ATK-002": "Tinta printer hitam original 70ml botol isi ulang untuk seri printer tangki tinta",
    "ATK-003": "Map plastik kancing ukuran folio warna campur, ketebalan 0,2mm, isi 12 per pak",
    "ATK-004": "Pulpen gel 0.5mm tinta hitam cepat kering dengan grip karet, isi 12 per lusin",
    "IT-001": "Mouse wireless 2.4GHz dengan receiver USB nano, 1600 DPI, termasuk baterai AA",
    "IT-002": "Keyboard USB full size layout US dengan tombol multimedia dan kaki pengatur tinggi",
    "IT-003": "Kabel LAN Cat6 UTP 23AWG tembaga murni, dijual per meter dari gulungan 305 meter",
    "IT-004": "SSD 512GB SATA III 2.5 inci, kecepatan baca hingga 550MB/s, garansi resmi 3 tahun",
    "SFT-001": "Helm proyek standar SNI dengan suspensi ratchet dan tali dagu, warna putih",
    "SFT-002": "Sepatu safety ujung baja anti slip dan tahan minyak, sol PU, ukuran sesuai daftar",
    "SFT-003": "Rompi reflektif warna hijau stabilo dengan dua strip reflektor dan resleting depan",
    "CLN-001": "Cairan pembersih lantai konsentrat aroma lemon kemasan jerigen 5 liter",
    "CLN-002": "Tisu gulung dua lapis tanpa pewangi, pak isi 10 gulung, untuk toilet kantor",
    "LOG-001": "Palet kayu ukuran 120x100cm, kayu kering oven bersertifikat fumigasi ISPM 15",
    "LOG-002": "Stretch film lebar 50cm tebal 17 mikron, panjang 300 meter per gulung",
}

_NAME_A = ["Maju", "Sumber", "Karya", "Mitra", "Cahaya", "Bintang", "Sinar", "Prima"]
_NAME_B = ["Jaya", "Abadi", "Sentosa", "Makmur", "Sejahtera", "Mandiri", "Utama", "Nusantara"]
_NAME_C = ["Teknik", "Niaga", "Logistik", "Supplies", "Perkasa", "Indah"]
_CITIES = ["Jakarta", "Surabaya", "Bandung", "Semarang", "Medan", "Makassar", "Sidoarjo"]
_BANKS = ["BRI", "BNI", "Mandiri", "BCA", "BSI"]


@dataclass(frozen=True)
class Dataset:
    vendors: list[Vendor]
    purchase_orders: list[PurchaseOrder]
    goods_receipts: list[GoodsReceipt]
    labels: list[InvoiceLabel]


def _digits(rng: random.Random, n: int) -> str:
    return str(rng.randint(1, 9)) + "".join(rng.choice("0123456789") for _ in range(n - 1))


def _rand_date(rng: random.Random, start: dt.date, end: dt.date) -> dt.date:
    return start + dt.timedelta(days=rng.randint(0, (end - start).days))


def make_vendors(rng: random.Random, n: int) -> list[Vendor]:
    names: set[str] = set()
    vendors: list[Vendor] = []
    while len(vendors) < n:
        name = (
            f"{rng.choice(['PT', 'CV'])} {rng.choice(_NAME_A)} {rng.choice(_NAME_B)} "
            f"{rng.choice(_NAME_C)}"
        )
        if name in names:
            continue
        names.add(name)
        vendors.append(
            Vendor(
                vendor_id=f"V{len(vendors) + 1:03d}",
                name=name,
                npwp=_digits(rng, 16),
                address=f"Jl. {rng.choice(_NAME_B)} No. {rng.randint(1, 200)}, "
                f"{rng.choice(_CITIES)}",
                bank_name=rng.choice(_BANKS),
                bank_account=_digits(rng, rng.choice([10, 13])),
            )
        )
    return vendors


def make_po(rng: random.Random, cfg: SynthConfig, seq: int, vendor: Vendor) -> PurchaseOrder:
    n_lines = rng.randint(1, min(cfg.max_lines_per_po, len(CATALOG)))
    items = rng.sample(CATALOG, n_lines)
    lines = []
    for sku, desc, lo, hi in items:
        qty = rng.randint(1, 50)
        price = rng.randint(lo // 100, hi // 100) * 100
        # Only draw from rng when enabled, so configs without it reproduce v1 exactly.
        if cfg.long_description_rate > 0 and rng.random() < cfg.long_description_rate:
            desc = LONG_DESCRIPTIONS[sku]
        lines.append(POLine(sku=sku, description=desc, qty=qty, unit_price=price))
    date = _rand_date(rng, cfg.start_date, cfg.end_date)
    return PurchaseOrder(
        po_number=f"PO/{date.year}/{seq:05d}", vendor_id=vendor.vendor_id, date=date, lines=lines
    )


def make_receipt(rng: random.Random, cfg: SynthConfig, po: PurchaseOrder, seq: int) -> GoodsReceipt:
    lines = []
    for ln in po.lines:
        partial = ln.qty > 1 and rng.random() < cfg.partial_delivery_rate
        lines.append(
            GRLine(sku=ln.sku, qty_received=rng.randint(1, ln.qty - 1) if partial else ln.qty)
        )
    return GoodsReceipt(
        gr_number=f"GR/{po.date.year}/{seq:05d}",
        po_number=po.po_number,
        date=po.date + dt.timedelta(days=rng.randint(1, 20)),
        lines=lines,
    )


def make_clean_invoice(
    rng: random.Random,
    cfg: SynthConfig,
    po: PurchaseOrder,
    gr: GoodsReceipt,
    vendor: Vendor,
    vendor_seq: int,
    invoice_id: str,
) -> Invoice:
    received = {ln.sku: ln.qty_received for ln in gr.lines}
    lines = [
        InvoiceLine(
            sku=ln.sku,
            description=ln.description,
            qty=received[ln.sku],
            unit_price=ln.unit_price,
            amount=0,
        )
        for ln in po.lines
        if received[ln.sku] > 0
    ]
    date = gr.date + dt.timedelta(days=rng.randint(0, 14))
    code = vendor.vendor_id
    inv = Invoice(
        invoice_id=invoice_id,
        invoice_number=f"INV/{code}/{date.year}/{vendor_seq:04d}",
        tax_invoice_number=_digits(rng, 17),
        invoice_date=date,
        po_number=po.po_number,
        vendor_name=vendor.name,
        vendor_npwp=vendor.npwp,
        bank_name=vendor.bank_name,
        bank_account=vendor.bank_account,
        lines=lines,
        dpp=0,
        vat=0,
        total=0,
    )
    return recompute_totals(inv, cfg.vat_bps)


def _pick_anomaly(rng: random.Random, cfg: SynthConfig) -> AnomalyType | None:
    if rng.random() >= cfg.anomaly_rate:
        return None
    kinds = list(cfg.anomaly_weights)
    return rng.choices(kinds, weights=[cfg.anomaly_weights[k] for k in kinds])[0]


def generate(cfg: SynthConfig) -> Dataset:
    rng = random.Random(cfg.seed)
    vendors = make_vendors(rng, cfg.n_vendors)
    by_id = {v.vendor_id: v for v in vendors}
    vendor_seq = dict.fromkeys(by_id, 0)

    pos: list[PurchaseOrder] = []
    grs: list[GoodsReceipt] = []
    labels: list[InvoiceLabel] = []

    def next_id() -> str:
        return f"inv-{len(labels) + 1:05d}"

    for seq in range(1, cfg.n_purchase_orders + 1):
        vendor = rng.choice(vendors)
        po = make_po(rng, cfg, seq, vendor)
        gr = make_receipt(rng, cfg, po, seq)
        pos.append(po)
        grs.append(gr)

        vendor_seq[vendor.vendor_id] += 1
        inv = make_clean_invoice(rng, cfg, po, gr, vendor, vendor_seq[vendor.vendor_id], next_id())
        kind = _pick_anomaly(rng, cfg)

        if kind is AnomalyType.DUPLICATE_INVOICE:
            labels.append(InvoiceLabel(invoice_id=inv.invoice_id, invoice=inv, anomalies=[]))
            # Re-submit an earlier invoice that was itself clean.
            originals = [lb for lb in labels if not lb.anomalies and lb.duplicate_of is None]
            original = rng.choice(originals)
            dup_id = next_id()
            dup = original.invoice.model_copy(update={"invoice_id": dup_id})
            labels.append(
                InvoiceLabel(
                    invoice_id=dup_id,
                    invoice=dup,
                    anomalies=[AnomalyType.DUPLICATE_INVOICE],
                    duplicate_of=original.invoice_id,
                    note=f"resubmission of {original.invoice_id}",
                )
            )
            continue

        note = ""
        anomalies: list[AnomalyType] = []
        if kind is not None:
            inv, note = INJECTORS[kind](inv, vendor, gr, rng, cfg.vat_bps)
            anomalies = [kind]
        labels.append(
            InvoiceLabel(invoice_id=inv.invoice_id, invoice=inv, anomalies=anomalies, note=note)
        )

    return Dataset(vendors=vendors, purchase_orders=pos, goods_receipts=grs, labels=labels)
