"""Contrato MarketRegimeAssessment — sección 4.10.3 del PDF maestro.

Este módulo define el contrato de datos que el Market Regime Engine produce y
que los módulos downstream (Feature Engineering, Context Builder, Decision
Aggregator) consumen.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.market_data.schemas import _ALLOWED_SYMBOLS

# ---------------------------------------------------------------------------
# Versión del contrato
# ---------------------------------------------------------------------------

REGIME_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PrimaryRegime(StrEnum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    UNCLEAR = "UNCLEAR"


class VolatilityState(StrEnum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class LiquidityState(StrEnum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class FundingState(StrEnum):
    POSITIVE = "POSITIVE"  # longs pagan shorts
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"  # shorts pagan longs
    EXTREME = "EXTREME"  # magnitud muy alta en cualquier dirección


class OpenInterestState(StrEnum):
    RISING = "RISING"
    STABLE = "STABLE"
    FALLING = "FALLING"
    UNKNOWN = "UNKNOWN"  # sin datos disponibles


class TrendAlignment(StrEnum):
    BULLISH = "BULLISH"  # dirección alcista multi-timeframe
    BEARISH = "BEARISH"  # dirección bajista multi-timeframe
    MIXED = "MIXED"  # señales contradictorias entre timeframes
    NEUTRAL = "NEUTRAL"  # sin tendencia clara


# ---------------------------------------------------------------------------
# Contrato principal
# ---------------------------------------------------------------------------


class MarketRegimeAssessment(BaseModel):
    """Contrato completo del output del Market Regime Engine (sección 4.10.3).

    Inmutable una vez construido. Consume datos de un MarketSnapshot y produce
    una clasificación de régimen reproducible y auditable para los módulos
    downstream.
    """

    # Identificación
    regime_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    snapshot_id: str
    symbol: str
    timestamp_utc: datetime

    # Clasificación de régimen
    primary_regime: PrimaryRegime
    secondary_regime: PrimaryRegime | None = None
    regime_confidence: float = Field(ge=0.0, le=1.0)

    # Estados auxiliares
    volatility_state: VolatilityState
    liquidity_state: LiquidityState
    funding_state: FundingState
    open_interest_state: OpenInterestState
    trend_alignment: TrendAlignment

    # Metadatos
    notes: list[str] = Field(default_factory=list)
    version: str = REGIME_VERSION

    model_config = {"frozen": True}

    # ------------------------------------------------------------------
    # Validadores de campo
    # ------------------------------------------------------------------

    @field_validator("regime_id", "snapshot_id")
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

    # ------------------------------------------------------------------
    # Serialización a tabla market_regimes
    # ------------------------------------------------------------------

    def to_db_kwargs(self, bot_run_id: str) -> dict[str, Any]:
        """Devuelve un dict listo para crear un registro en market_regimes."""
        details: dict[str, Any] = {
            "secondary_regime": self.secondary_regime,
            "volatility_state": self.volatility_state,
            "liquidity_state": self.liquidity_state,
            "funding_state": self.funding_state,
            "open_interest_state": self.open_interest_state,
            "trend_alignment": self.trend_alignment,
            "notes": self.notes,
            "version": self.version,
        }
        return {
            "id": self.regime_id,
            "bot_run_id": bot_run_id,
            "market_snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp_utc,
            "regime": self.primary_regime,
            "confidence": self.regime_confidence,
            "regime_details": details,
        }
