"""Schemas Pydantic para el módulo de Historical Replay (F11)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator

from backend.market_data.schemas import ALLOWED_SYMBOLS


class SnapshotWindow(BaseModel):
    """Ventana temporal para cargar snapshots históricos."""

    symbol: str
    period_start: datetime
    period_end: datetime

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        if v not in ALLOWED_SYMBOLS:
            raise ValueError(f"Símbolo '{v}' no permitido. Válidos: {sorted(ALLOWED_SYMBOLS)}")
        return v

    @model_validator(mode="after")
    def end_after_start(self) -> SnapshotWindow:
        if self.period_end <= self.period_start:
            raise ValueError("period_end debe ser posterior a period_start")
        return self
