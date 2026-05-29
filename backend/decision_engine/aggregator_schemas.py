"""Contrato DecisionAggregationResult — sección 4.10.8 del PDF maestro.

Output inmutable del Decision Aggregator. Combina scores de fuentes
heterogéneas (quant, GPT, régimen de mercado, volatilidad) en un
score agregado único y registra las contradicciones detectadas entre
fuentes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.decision_engine.schemas import DecisionType
from backend.market_data.schemas import _ALLOWED_SYMBOLS

AGGREGATION_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Sub-modelos
# ---------------------------------------------------------------------------


class ContributingSources(BaseModel):
    """Scores individuales de cada fuente que alimenta el aggregator."""

    quant_score: float = Field(ge=0.0, le=1.0)
    gpt_context_score: float = Field(ge=0.0, le=1.0)
    regime_factor: float = Field(ge=0.0, le=1.0)
    volatility_factor: float = Field(ge=0.0, le=1.0)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Contrato principal
# ---------------------------------------------------------------------------


class DecisionAggregationResult(BaseModel):
    """Contrato de salida del Decision Aggregator (sección 4.10.8).

    Inmutable una vez construido. Combina scores de las épicas upstream
    (F5: quant, F7: GPT context, F6: régimen y volatilidad) en un
    aggregated_score final y lista las contradicciones detectadas entre
    fuentes.

    aggregated_score en [0.0, 1.0]: mayor = señal de mayor calidad.
    contributing_sources expone el desglose por fuente para auditoría.
    contradictions_detected lista los conflictos lógicos entre fuentes.
    """

    # Identificación
    aggregation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    decision_id: str  # UUID del ModelDecision que originó esta agregación
    symbol: str
    timestamp_utc: datetime

    # Fuentes contributivas (desglose por fuente para auditoría)
    contributing_sources: ContributingSources

    # Score final agregado [0.0, 1.0]
    aggregated_score: float = Field(ge=0.0, le=1.0)

    # Acción final recomendada por el aggregator
    final_action: DecisionType

    # Conflictos lógicos detectados entre fuentes
    contradictions_detected: list[str] = Field(default_factory=list)

    # Versionado
    version: str = AGGREGATION_VERSION

    model_config = {"frozen": True}

    # ------------------------------------------------------------------
    # Validadores
    # ------------------------------------------------------------------

    @field_validator("aggregation_id", "decision_id")
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
    # Serialización a tabla decision_aggregations
    # ------------------------------------------------------------------

    def to_db_kwargs(self, bot_run_id: str) -> dict[str, Any]:
        """Devuelve un dict listo para crear un registro en decision_aggregations."""
        return {
            "id": self.aggregation_id,
            "bot_run_id": bot_run_id,
            "decision_id": self.decision_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp_utc,
            "quant_score": self.contributing_sources.quant_score,
            "gpt_confidence": self.contributing_sources.gpt_context_score,
            "regime_factor": self.contributing_sources.regime_factor,
            "volatility_factor": self.contributing_sources.volatility_factor,
            "aggregated_score": self.aggregated_score,
            "final_action": self.final_action.value,
            "reasons": {
                "contradictions_detected": self.contradictions_detected,
                "version": self.version,
            },
        }
