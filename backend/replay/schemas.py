"""Schemas Pydantic para el módulo de Historical Replay (F11)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

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


# ---------------------------------------------------------------------------
# Schemas del comparador de decisiones (entonces vs ahora)
# ---------------------------------------------------------------------------


class FieldDelta(BaseModel):
    """Diferencia de un campo entre la decisión histórica (entonces) y el replay actual (ahora)."""

    field: str
    entonces: str | None
    ahora: str | None
    changed: bool

    model_config = {"frozen": True}


class ComparisonStepResult(BaseModel):
    """Resultado de comparación para un único snapshot."""

    snapshot_id: str
    snapshot_timestamp: datetime

    # Valores históricos registrados en DB ("entonces")
    historical_action: str | None = None
    historical_final_action: str | None = None
    historical_aggregated_score: float | None = None
    historical_risk_result: str | None = None
    # True cuando no se encontró registro histórico para este snapshot
    historical_missing: bool = False

    # Valores producidos por el pipeline actual ("ahora")
    replay_action: str
    replay_final_action: str
    replay_aggregated_score: float
    replay_risk_result: str

    deltas: list[FieldDelta] = Field(default_factory=list)
    any_changed: bool = False

    model_config = {"frozen": True}


class ComparisonReport(BaseModel):
    """Resumen de la comparación entonces vs ahora para una ventana completa."""

    window: SnapshotWindow
    bot_run_id: str | None = None
    total_steps: int
    # Pasos donde al menos un campo difiere respecto al registro histórico
    changed_steps: int
    # Pasos sin registro histórico en DB
    missing_steps: int
    change_rate: float = Field(ge=0.0, le=1.0)
    steps: list[ComparisonStepResult] = Field(default_factory=list)

    model_config = {"frozen": True}
