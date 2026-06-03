"""Contrato RiskValidationResult — sección 4.10.9 del PDF maestro.

Output inmutable del Risk Engine. Recibe un DecisionAggregationResult de F8
y emite una de cuatro decisiones: APPROVE / ADJUST_DOWN / BLOCK / NO_OPERAR,
con los parámetros ajustados y los motivos que fundamentan la decisión.

Distinción clave (regla no negociable del proyecto):
- NO_OPERAR: el Decision Aggregator detectó ausencia de edge (execute=False).
  El Risk Engine propaga la decisión sin evaluarla; no es un rechazo.
- BLOCK: el Risk Engine rechazó un trade que el Aggregator quería ejecutar
  (execute=True) porque violó al menos una regla de riesgo.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.market_data.schemas import ALLOWED_SYMBOLS

RISK_VALIDATION_VERSION = "1.0"

# No-negociable del proyecto: máximo 10 USDT de margen por operación
MAX_MARGIN_USDT = Decimal("10.0")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RiskDecision(StrEnum):
    """Resultado de la validación de riesgo emitida por el Risk Engine.

    NO_OPERAR y BLOCK son estados distintos y no deben mezclarse:
    - NO_OPERAR: el Decision Aggregator determinó que no hay edge suficiente
      para operar (decision.execute=False). El Risk Engine no evalúa el trade;
      lo propaga tal como llega para auditoría completa.
    - BLOCK: el Risk Engine rechazó un trade que el Aggregator quería ejecutar
      (decision.execute=True) porque violó una o más reglas de riesgo.
    """

    APPROVE = "APPROVE"  # Trade aprobado sin cambios
    ADJUST_DOWN = "ADJUST_DOWN"  # Trade aprobado con parámetros reducidos
    BLOCK = "BLOCK"  # Trade rechazado por el Risk Engine
    NO_OPERAR = "NO_OPERAR"  # Sin edge — decisión del Aggregator, no del Risk Engine


# ---------------------------------------------------------------------------
# Sub-modelos
# ---------------------------------------------------------------------------


class AdjustedParameters(BaseModel):
    """Parámetros finales tras aplicar los ajustes del Risk Engine.

    Presente en APPROVE y ADJUST_DOWN. Ausente (None) cuando BLOCK o NO_OPERAR.
    En APPROVE los valores son idénticos a los originales; en ADJUST_DOWN
    al menos uno es menor al original.
    """

    margin_usdt: Decimal = Field(gt=Decimal("0"), le=MAX_MARGIN_USDT)
    leverage: int = Field(ge=1, le=10)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Contrato principal
# ---------------------------------------------------------------------------


class RiskValidationResult(BaseModel):
    """Contrato de salida del Risk Engine (sección 4.10.9).

    Inmutable una vez construido.

    Decisiones:
    - APPROVE: trade aceptado con los parámetros originales de F8.
    - ADJUST_DOWN: trade aceptado con margin y/o leverage reducidos.
    - BLOCK: trade rechazado por el Risk Engine; adjusted_parameters es None.
    - NO_OPERAR: sin edge según el Aggregator; adjusted_parameters es None.
      No confundir con BLOCK: NO_OPERAR no es un rechazo del Risk Engine.

    reasons mapea nombre-de-regla → explicación legible, para auditoría
    completa de cada decisión.
    """

    # Identificación
    validation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    aggregation_id: str  # UUID del DecisionAggregationResult que se validó
    symbol: str
    timestamp_utc: datetime

    # Decisión final del Risk Engine
    decision: RiskDecision

    # Parámetros originales (tal como llegaron de ModelDecision vía F8).
    # Para NO_OPERAR margin=0 es válido (no hay trade); para el resto debe ser > 0.
    # La restricción estricta se aplica en el model_validator.
    original_margin_usdt: Decimal = Field(ge=Decimal("0"), le=MAX_MARGIN_USDT)
    original_leverage: int = Field(ge=1, le=10)

    # Parámetros finales tras ajustes — None cuando BLOCK o NO_OPERAR
    adjusted_parameters: AdjustedParameters | None = None

    # Snapshot de pérdidas acumuladas al momento de la validación
    daily_loss_at_check_usdt: Decimal | None = Field(default=None, ge=Decimal("0"))
    total_loss_at_check_usdt: Decimal | None = Field(default=None, ge=Decimal("0"))

    # Motivos de la decisión: rule_name → explicación legible
    reasons: dict[str, str] = Field(min_length=1)

    version: str = RISK_VALIDATION_VERSION

    model_config = {"frozen": True}

    # ------------------------------------------------------------------
    # Validadores de campo
    # ------------------------------------------------------------------

    @field_validator("validation_id", "aggregation_id")
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
        if v not in ALLOWED_SYMBOLS:
            raise ValueError(f"Símbolo '{v}' no permitido. Válidos: {sorted(ALLOWED_SYMBOLS)}")
        return v

    @field_validator("timestamp_utc")
    @classmethod
    def must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() != timedelta(0):
            raise ValueError("timestamp_utc debe ser UTC (offset=0).")
        return v

    # ------------------------------------------------------------------
    # Validador de modelo (coherencia entre campos)
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def adjusted_parameters_coherent(self) -> RiskValidationResult:
        """Garantiza coherencia entre decision y adjusted_parameters.

        BLOCK y NO_OPERAR son las únicas decisiones sin parámetros ajustados.
        NO_OPERAR indica ausencia de edge (decisión del Aggregator);
        BLOCK indica rechazo del Risk Engine. No son equivalentes.

        Para decisiones ejecutables (APPROVE / ADJUST_DOWN / BLOCK),
        original_margin_usdt debe ser > 0. Para NO_OPERAR, puede ser 0
        (no hay trade que ejecutar).
        """
        no_params_decisions = {RiskDecision.BLOCK, RiskDecision.NO_OPERAR}
        if self.decision in no_params_decisions and self.adjusted_parameters is not None:
            raise ValueError(
                f"adjusted_parameters debe ser None cuando decision es {self.decision}."
            )
        if self.decision not in no_params_decisions and self.adjusted_parameters is None:
            raise ValueError(
                "adjusted_parameters es obligatorio cuando decision es APPROVE o ADJUST_DOWN."
            )
        # Para decisiones que implican un trade real, el margen debe ser > 0.
        if self.decision != RiskDecision.NO_OPERAR and self.original_margin_usdt <= 0:
            raise ValueError(
                "original_margin_usdt debe ser > 0 para decisiones ejecutables "
                f"(decision={self.decision})."
            )
        if self.decision == RiskDecision.ADJUST_DOWN and self.adjusted_parameters is not None:
            reduced_margin = self.adjusted_parameters.margin_usdt < self.original_margin_usdt
            reduced_leverage = self.adjusted_parameters.leverage < self.original_leverage
            if not (reduced_margin or reduced_leverage):
                raise ValueError(
                    "ADJUST_DOWN requiere que al menos un parámetro (margin o leverage) "
                    "sea menor al original."
                )
        return self

    # ------------------------------------------------------------------
    # Serialización hacia la tabla risk_validations
    # ------------------------------------------------------------------

    def to_db_kwargs(self, bot_run_id: str) -> dict[str, Any]:
        """Devuelve un dict listo para crear un registro en risk_validations.

        Args:
            bot_run_id: UUID del BotRun activo con FK a bot_runs.id.
        """
        try:
            uuid.UUID(bot_run_id)
        except ValueError as exc:
            raise ValueError(f"'{bot_run_id}' no es un UUID válido.") from exc
        adj = self.adjusted_parameters
        # `version` excluido intencionalmente: confirmar columna en ORM (F10/F11).
        return {
            "id": self.validation_id,
            "bot_run_id": bot_run_id,
            "decision_aggregation_id": self.aggregation_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp_utc,
            "result": self.decision.value,
            "original_margin": self.original_margin_usdt,
            "original_leverage": self.original_leverage,
            "adjusted_margin": adj.margin_usdt if adj else None,
            "adjusted_leverage": adj.leverage if adj else None,
            "daily_loss_at_check": self.daily_loss_at_check_usdt,
            "total_loss_at_check": self.total_loss_at_check_usdt,
            "reasons": self.reasons,
        }
