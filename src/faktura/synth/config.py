"""Generator configuration, loaded from YAML."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from faktura.schemas import AnomalyType

LAYOUTS = ("classic", "modern", "compact", "borderless")
DATE_STYLES = ("short", "long", "iso", "mon")
MONEY_STYLES = ("id_rp", "id_rp_dec", "idr_comma")


class RenderConfig(BaseModel):
    """How invoices look. Defaults reproduce the v1 look (one layout, mild scans)."""

    layout_weights: dict[str, float] = Field(default_factory=lambda: {"classic": 1.0})
    date_styles: list[str] = Field(default_factory=lambda: ["short", "long"])
    money_styles: list[str] = Field(default_factory=lambda: ["id_rp"])
    # Degradation level -> weight. 0 clean, 1 mild scan, 2 poor scan, 3 phone photo.
    # None keeps v1 behaviour: level 1 with probability `scan_effect_rate`, else 0.
    level_weights: dict[int, float] | None = None
    # Probability of a "DITERIMA" stamp over the totals, for levels >= 2.
    stamp_rate: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> RenderConfig:
        unknown = (
            (set(self.layout_weights) - set(LAYOUTS))
            | (set(self.date_styles) - set(DATE_STYLES))
            | (set(self.money_styles) - set(MONEY_STYLES))
        )
        if unknown:
            raise ValueError(f"unknown render options: {sorted(unknown)}")
        if self.level_weights is not None and not set(self.level_weights) <= {0, 1, 2, 3}:
            raise ValueError("level_weights keys must be 0..3")
        return self


class SynthConfig(BaseModel):
    seed: int = 42
    n_vendors: int = Field(default=12, ge=1)
    n_purchase_orders: int = Field(default=200, ge=1)
    start_date: dt.date = dt.date(2025, 1, 1)
    end_date: dt.date = dt.date(2025, 12, 31)
    # Effective VAT in basis points. 1100 = 11%; see docs/assumptions.md.
    vat_bps: int = Field(default=1100, ge=0)
    max_lines_per_po: int = Field(default=6, ge=1)
    partial_delivery_rate: float = Field(default=0.2, ge=0, le=1)
    # Share of invoices that carry exactly one anomaly (duplicates are extra files).
    anomaly_rate: float = Field(default=0.3, ge=0, le=1)
    anomaly_weights: dict[AnomalyType, float] = Field(
        default_factory=lambda: {
            AnomalyType.PRICE_MISMATCH: 1.0,
            AnomalyType.QTY_OVER_RECEIVED: 1.0,
            AnomalyType.DUPLICATE_INVOICE: 1.0,
            AnomalyType.NPWP_MISMATCH: 0.7,
            AnomalyType.VAT_MISCALC: 0.7,
            AnomalyType.BANK_ACCOUNT_CHANGED: 0.6,
        }
    )
    scan_effect_rate: float = Field(default=0.5, ge=0, le=1)
    # Share of PO lines that use a long, wrapping description. 0 keeps v1 data identical.
    long_description_rate: float = Field(default=0.0, ge=0, le=1)
    render: RenderConfig = Field(default_factory=RenderConfig)

    @model_validator(mode="after")
    def _check(self) -> SynthConfig:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if not self.anomaly_weights or any(w < 0 for w in self.anomaly_weights.values()):
            raise ValueError("anomaly_weights must be non-empty and non-negative")
        return self


def load_config(path: Path) -> SynthConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SynthConfig.model_validate(raw)
