"""Contrato VolatilityAssessmentPackage — épica F6 (sección 4.10.x del PDF maestro).

Limitación conocida: MarketSnapshot expone una sola vela por timeframe, por lo que:
- ATR ≈ High − Low (True Range sin prev_close).
- realized_vol ≈ media de |ROC intra-vela| en los 4 TFs (proxy, no serie temporal).
Esta aproximación es aceptable mientras el contrato MarketSnapshot no exponga
historia de velas por TF.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.market_data.schemas import _ALLOWED_SYMBOLS

# ---------------------------------------------------------------------------
# Versión
# ---------------------------------------------------------------------------

VOLATILITY_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class VolatilityRegime(StrEnum):
    EXPANSION = "EXPANSION"
    CONTRACTION = "CONTRACTION"


# ---------------------------------------------------------------------------
# Contrato principal
# ---------------------------------------------------------------------------


class VolatilityAssessmentPackage(BaseModel):
    """Output del Volatility Engine para un MarketSnapshot dado.

    Inmutable una vez construido. Downstream: Feature Engineering, Risk Engine,
    Decision Aggregator.
    """

    # Identificación
    assessment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    snapshot_id: str
    timestamp_utc: datetime
    symbol: str

    # ATR por timeframe (H − L, aproximación de True Range con vela única)
    atr_5m: Decimal = Field(ge=0)
    atr_15m: Decimal = Field(ge=0)
    atr_1h: Decimal = Field(ge=0)
    atr_4h: Decimal = Field(ge=0)

    # ATR 1h como porcentaje del last_price (referencia principal)
    atr_percent: float = Field(ge=0.0)

    # Realized vol proxy: media de |ROC intra-vela| en los 4 TFs
    realized_vol: float = Field(ge=0.0)

    # Clasificación de régimen
    volatility_regime: VolatilityRegime

    # Scores normalizados
    volatility_score: float = Field(ge=0.0, le=1.0)
    liquidation_risk_score: float = Field(ge=0.0, le=1.0)

    # Cap de leverage recomendado basado en volatilidad
    leverage_cap: int = Field(ge=1)

    # Breakdown auditable
    details: dict[str, Any]

    version: str = VOLATILITY_VERSION

    model_config = {"frozen": True}

    # ------------------------------------------------------------------
    # Validadores
    # ------------------------------------------------------------------

    @field_validator("assessment_id", "snapshot_id")
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
    # Serialización a tabla volatility_assessments
    # ------------------------------------------------------------------

    def to_db_kwargs(self, bot_run_id: str) -> dict[str, Any]:
        """Devuelve un dict listo para crear un registro en volatility_assessments.

        La columna `atr` almacena el ATR de referencia (1h). Los ATRs por TF
        y el resto de metadatos van en `details`.
        """
        details_full: dict[str, Any] = {
            "atr_5m": str(self.atr_5m),
            "atr_15m": str(self.atr_15m),
            "atr_1h": str(self.atr_1h),
            "atr_4h": str(self.atr_4h),
            "realized_vol": self.realized_vol,
            "volatility_regime": self.volatility_regime,
            "version": self.version,
            **self.details,
        }

        return {
            "id": self.assessment_id,
            "bot_run_id": bot_run_id,
            "market_snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp_utc,
            "atr": self.atr_1h,
            "atr_percent": self.atr_percent,
            "volatility_score": self.volatility_score,
            "leverage_cap": self.leverage_cap,
            "liquidation_risk_score": self.liquidation_risk_score,
            "details": details_full,
        }
