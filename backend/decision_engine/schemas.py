"""JSON Schema de decisión — sección 3.8 del spec maestro.

Define el contrato que el GPT debe cumplir en cada respuesta. Este schema
es inmutable, versionado y la única fuente de verdad para la estructura de
output del modelo. El Risk Engine consume este contrato; nunca accede al
JSON raw.

Restricciones no-negociables embebidas:
- symbol: whitelist de 5 pares permitidos
- margin_usdt: hard cap de 10 USDT
- leverage: 1–10 global; caps por entorno (PAPER ≤10, TESTNET/LIVE ≤5)
- stop_loss y take_profit: obligatorios cuando decision == OPEN
- direction: obligatorio cuando decision == OPEN
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.core.config import Environment
from backend.market_data.schemas import ALLOWED_SYMBOLS

# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

DECISION_SCHEMA_VERSION = "1.0"

# Leverage caps por entorno (alineados con LeverageConfig en config.yaml)
_LEVERAGE_CAP: dict[Environment, int] = {
    Environment.PAPER: 10,
    Environment.TESTNET: 5,
    Environment.LIVE: 5,
}

_MAX_MARGIN_USDT = Decimal("10")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DecisionAction(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    NO_OPERAR = "NO_OPERAR"


class TradeDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


# ---------------------------------------------------------------------------
# Schema principal
# ---------------------------------------------------------------------------


class GPTDecisionResponse(BaseModel):
    """Contrato JSON del output del GPT (sección 3.8).

    Inmutable una vez validado. Producido por el GPT, consumido por
    DecisionAggregator y RiskEngine. El Risk Engine tiene la palabra final;
    este schema solo garantiza que el output es parseable y coherente.
    """

    # Identificación del ciclo
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # Contexto que GPT debe ecoear (auditabilidad)
    challenge_mode: str = Field(min_length=1)
    environment: Environment

    # Decisión principal
    decision: DecisionAction
    symbol: str

    # Campos requeridos sólo cuando decision == OPEN
    direction: TradeDirection | None = None
    leverage: int | None = Field(default=None, ge=1, le=10)
    margin_usdt: Decimal | None = Field(default=None, gt=Decimal("0"))
    stop_loss: Decimal | None = Field(default=None, gt=Decimal("0"))
    take_profit: Decimal | None = Field(default=None, gt=Decimal("0"))

    # Calidad de la señal [0.0, 1.0]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    # Razonamiento (siempre obligatorio — auditabilidad)
    reasoning: str = Field(min_length=10)

    # Versionado del schema
    schema_version: str = DECISION_SCHEMA_VERSION

    model_config = {"frozen": True}

    # ------------------------------------------------------------------
    # Validadores de campo
    # ------------------------------------------------------------------

    @field_validator("decision_id")
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

    # ------------------------------------------------------------------
    # Validadores de modelo (cross-field)
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def open_requires_full_spec(self) -> GPTDecisionResponse:
        """OPEN exige direction, leverage, margin_usdt, stop_loss y take_profit."""
        if self.decision != DecisionAction.OPEN:
            return self

        missing = [
            field
            for field, val in [
                ("direction", self.direction),
                ("leverage", self.leverage),
                ("margin_usdt", self.margin_usdt),
                ("stop_loss", self.stop_loss),
                ("take_profit", self.take_profit),
            ]
            if val is None
        ]
        if missing:
            raise ValueError(f"decision=OPEN requiere los campos: {missing}")
        return self

    @model_validator(mode="after")
    def margin_within_hard_cap(self) -> GPTDecisionResponse:
        """margin_usdt nunca puede superar el hard cap de 10 USDT."""
        if self.margin_usdt is not None and self.margin_usdt > _MAX_MARGIN_USDT:
            raise ValueError(
                f"margin_usdt={self.margin_usdt} supera el hard cap de {_MAX_MARGIN_USDT} USDT"
            )
        return self

    @model_validator(mode="after")
    def leverage_within_env_cap(self) -> GPTDecisionResponse:
        """Leverage no puede superar el cap del entorno actual."""
        if self.leverage is None:
            return self
        cap = _LEVERAGE_CAP[self.environment]
        if self.leverage > cap:
            raise ValueError(
                f"leverage={self.leverage}x supera el cap de {cap}x para {self.environment}"
            )
        return self

    @model_validator(mode="after")
    def sl_below_tp_on_long(self) -> GPTDecisionResponse:
        """OPEN LONG: stop_loss < take_profit. OPEN SHORT: stop_loss > take_profit."""
        if self.decision != DecisionAction.OPEN:
            return self
        if self.stop_loss is None or self.take_profit is None:
            return self  # ya capturado por open_requires_full_spec

        if self.direction == TradeDirection.LONG and self.stop_loss >= self.take_profit:
            raise ValueError(
                f"LONG: stop_loss={self.stop_loss} debe ser menor que"
                f" take_profit={self.take_profit}"
            )
        if self.direction == TradeDirection.SHORT and self.stop_loss <= self.take_profit:
            raise ValueError(
                f"SHORT: stop_loss={self.stop_loss} debe ser mayor que"
                f" take_profit={self.take_profit}"
            )
        return self

    # ------------------------------------------------------------------
    # Serialización a tabla decisions
    # ------------------------------------------------------------------

    def to_db_kwargs(self, bot_run_id: str, model_response_id: str | None = None) -> dict[str, Any]:
        """Devuelve un dict listo para crear un registro en decisions."""
        return {
            "id": self.decision_id,
            "bot_run_id": bot_run_id,
            "model_response_id": model_response_id,
            "symbol": self.symbol,
            "action": self.decision,
            "direction": self.direction,
            "confidence": self.confidence,
            "margin_usdt": self.margin_usdt,
            "leverage": self.leverage,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "reasoning": self.reasoning,
            "raw_decision": self.model_dump(mode="json"),
        }
