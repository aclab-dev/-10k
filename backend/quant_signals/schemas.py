"""Contrato QuantSignalsPackage — sección 4.10.2 del PDF maestro.

Este módulo define el contrato de datos que el Quant Signals Engine produce y
que los módulos downstream (Feature Engineering, Context Builder, Decision
Aggregator) consumen. Es la representación inmutable del output cuantitativo
de un ciclo de análisis de señales para un símbolo dado.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.market_data.schemas import _ALLOWED_SYMBOLS

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

SIGNAL_VERSION = "1.0"

_ALLOWED_TIMEFRAMES = frozenset({"5m", "15m", "1h", "4h"})


# ---------------------------------------------------------------------------
# Contrato principal
# ---------------------------------------------------------------------------


class QuantSignalsPackage(BaseModel):
    """Contrato completo del output del Quant Signals Engine (sección 4.10.2).

    Inmutable una vez construido. Consume un MarketSnapshot y produce señales
    reproducibles y auditables para los módulos downstream.

    Señales individuales en [-1.0, 1.0]: negativo = bearish, positivo = bullish.
    Scores agregados en [0.0, 1.0].
    """

    # Identificación
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    snapshot_id: str  # UUID del MarketSnapshot que originó este paquete
    timestamp_utc: datetime
    symbol: str
    timeframes_used: list[str] = Field(min_length=1)

    # Señales individuales [-1.0, 1.0]
    momentum_signal: float | None = None
    mean_reversion_signal: float | None = None
    breakout_signal: float | None = None
    funding_signal: float | None = None
    open_interest_signal: float | None = None
    order_flow_imbalance_signal: float | None = None
    liquidity_sweep_signal: float | None = None

    # Scores agregados [0.0, 1.0]
    signal_strength_score: float | None = Field(default=None, ge=0.0, le=1.0)
    signal_conflict_score: float | None = Field(default=None, ge=0.0, le=1.0)
    signal_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    # Metadatos y versionado
    raw_feature_refs: dict[str, Any] | None = None
    version: str = SIGNAL_VERSION

    model_config = {"frozen": True}

    # ------------------------------------------------------------------
    # Validadores de campo
    # ------------------------------------------------------------------

    @field_validator("signal_id", "snapshot_id")
    @classmethod
    def must_be_valid_uuid(cls, v: str) -> str:
        try:
            uuid.UUID(v)
        except ValueError as exc:
            raise ValueError(f"'{v}' no es un UUID válido.") from exc
        return v

    @field_validator("symbol")
    @classmethod
    def symbol_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_SYMBOLS:
            raise ValueError(f"Símbolo '{v}' no permitido. Válidos: {sorted(_ALLOWED_SYMBOLS)}")
        return v

    @field_validator("timestamp_utc")
    @classmethod
    def must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() != timedelta(0):
            raise ValueError("timestamp_utc debe ser UTC (offset=0).")
        return v

    @field_validator("timeframes_used")
    @classmethod
    def timeframes_valid(cls, v: list[str]) -> list[str]:
        invalid = set(v) - _ALLOWED_TIMEFRAMES
        if invalid:
            raise ValueError(
                f"Timeframes inválidos: {sorted(invalid)}. Válidos: {sorted(_ALLOWED_TIMEFRAMES)}"
            )
        return v

    @field_validator(
        "momentum_signal",
        "mean_reversion_signal",
        "breakout_signal",
        "funding_signal",
        "open_interest_signal",
        "order_flow_imbalance_signal",
        "liquidity_sweep_signal",
        mode="before",
    )
    @classmethod
    def individual_signal_in_range(cls, v: float | None) -> float | None:
        if v is not None and not (-1.0 <= v <= 1.0):
            raise ValueError(f"Las señales individuales deben estar en [-1.0, 1.0], recibido: {v}")
        return v

    # ------------------------------------------------------------------
    # Serialización a tabla quant_signals
    # ------------------------------------------------------------------

    def to_db_kwargs(self, bot_run_id: str) -> dict[str, Any]:
        """Devuelve un dict listo para crear un registro en quant_signals.

        La mayoría de señales se mapean a columnas *_score en la tabla.
        Excepción: funding_signal y open_interest_signal conservan el sufijo
        _signal porque así se llaman las columnas en el ORM (models.QuantSignal).
        composite_score mapea a signal_strength_score (intensidad global de señal).
        signal_conflict_score y metadatos sin columna propia van en raw_signals.
        """
        raw_signals: dict[str, Any] = {
            "signal_conflict_score": self.signal_conflict_score,
            "signal_confidence": self.signal_confidence,
            "timeframes_used": self.timeframes_used,
            "version": self.version,
        }
        if self.raw_feature_refs:
            raw_signals["raw_feature_refs"] = self.raw_feature_refs

        return {
            "id": self.signal_id,
            "bot_run_id": bot_run_id,
            "market_snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp_utc,
            "momentum_score": self.momentum_signal,
            "mean_reversion_score": self.mean_reversion_signal,
            "breakout_score": self.breakout_signal,
            "liquidity_sweep_score": self.liquidity_sweep_signal,
            "order_flow_score": self.order_flow_imbalance_signal,
            # columnas ORM se llaman *_signal (no *_score) para estos dos campos
            "funding_signal": self.funding_signal,
            "open_interest_signal": self.open_interest_signal,
            "composite_score": self.signal_strength_score,
            "raw_signals": raw_signals,
        }
