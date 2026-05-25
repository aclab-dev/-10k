"""Schemas del Decision Engine — sección 3.8 del PDF maestro.

ModelDecision es el contrato JSON que el GPT Context Evaluator debe devolver.
El JSON Schema Guard valida este contrato antes de que la decisión llegue
al Risk Engine. Cualquier campo fuera de rango o enum inválido → BLOCK.
"""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.market_data.schemas import _ALLOWED_SYMBOLS

DECISION_SCHEMA_VERSION = "1.0"
CHALLENGE_MODE = "AUTONOMOUS_FUTURES_GPT55_QUANT_CONTROLLED_RISK"


# ---------------------------------------------------------------------------
# Enums — interpretaciones cualitativas de señales cuantitativas
# ---------------------------------------------------------------------------


class DecisionType(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_OPERAR = "NO_OPERAR"


class EntryType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    NO_ENTRY = "NO_ENTRY"


class MomentumInterpretation(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    UNCLEAR = "UNCLEAR"


class MeanReversionInterpretation(StrEnum):
    LONG_BIAS = "LONG_BIAS"
    SHORT_BIAS = "SHORT_BIAS"
    NEUTRAL = "NEUTRAL"
    UNCLEAR = "UNCLEAR"


class BreakoutInterpretation(StrEnum):
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    WATCH = "WATCH"
    NONE = "NONE"


class FundingInterpretation(StrEnum):
    SUPPORTS_TRADE = "SUPPORTS_TRADE"
    CONTRADICTS_TRADE = "CONTRADICTS_TRADE"
    NEUTRAL = "NEUTRAL"
    UNCLEAR = "UNCLEAR"


class OpenInterestInterpretation(StrEnum):
    RISING_WITH_PRICE = "RISING_WITH_PRICE"
    RISING_AGAINST_PRICE = "RISING_AGAINST_PRICE"
    FALLING = "FALLING"
    NEUTRAL = "NEUTRAL"
    UNCLEAR = "UNCLEAR"


class OrderFlowInterpretation(StrEnum):
    BUY_PRESSURE = "BUY_PRESSURE"
    SELL_PRESSURE = "SELL_PRESSURE"
    BALANCED = "BALANCED"
    UNAVAILABLE = "UNAVAILABLE"


class LiquiditySweepInterpretation(StrEnum):
    BUY_SIDE_SWEEP = "BUY_SIDE_SWEEP"
    SELL_SIDE_SWEEP = "SELL_SIDE_SWEEP"
    NONE = "NONE"
    UNCLEAR = "UNCLEAR"


class NewsImpact(StrEnum):
    SUPPORTS_TRADE = "SUPPORTS_TRADE"
    CONTRADICTS_TRADE = "CONTRADICTS_TRADE"
    NEUTRAL = "NEUTRAL"
    UNCLEAR = "UNCLEAR"


# ---------------------------------------------------------------------------
# Sub-modelos
# ---------------------------------------------------------------------------


class QuantSignalsSection(BaseModel):
    """Interpretación cualitativa de las señales cuantitativas calculadas."""

    momentum: MomentumInterpretation
    mean_reversion: MeanReversionInterpretation
    breakout_detection: BreakoutInterpretation
    funding_analysis: FundingInterpretation
    open_interest_analysis: OpenInterestInterpretation
    order_flow_imbalance: OrderFlowInterpretation
    liquidity_sweep: LiquiditySweepInterpretation


class DecisionAggregatorSection(BaseModel):
    """Scores combinados del Decision Aggregator."""

    quant_score: float = Field(ge=0.0, le=1.0)
    gpt_context_score: float = Field(ge=0.0, le=1.0)
    risk_quality_score: float = Field(ge=0.0, le=1.0)
    final_trade_quality_score: float = Field(ge=0.0, le=1.0)
    contradictions_detected: list[str] = Field(default_factory=list)


class NewsContextSection(BaseModel):
    """Contexto externo de noticias."""

    used: bool
    impact: NewsImpact
    summary: str


class PositionManagementPlan(BaseModel):
    """Plan de gestión de la posición abierta."""

    use_trailing_stop: bool
    move_to_break_even: bool
    partial_close_plan: str
    max_time_in_trade_minutes: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Contrato principal
# ---------------------------------------------------------------------------


class ModelDecision(BaseModel):
    """Decisión estructurada que GPT Context Evaluator debe devolver (sección 3.8).

    El JSON Schema Guard valida este modelo antes de que llegue al Risk Engine.
    Reglas de negocio obligatorias:
    - decision=NO_OPERAR → execute=False siempre
    - execute=True y decision=LONG/SHORT → stop_loss > 0 y take_profit > 0
    - margin_usdt ≤ 10 (límite absoluto; Risk Engine puede reducir más)
    - leverage ≤ 10 (máximo PAPER; Risk Engine aplica caps de entorno)
    - confidence ∈ [0.0, 1.0]
    """

    # Metadatos
    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    challenge_mode: str = Field(default=CHALLENGE_MODE)
    schema_version: str = Field(default=DECISION_SCHEMA_VERSION)
    environment: str
    timestamp_utc: str

    # Decisión
    decision: DecisionType
    symbol: str
    market: str = Field(default="USDT_M_FUTURES")
    exchange_preference: str = Field(default="BINGX")
    margin_type: str = Field(default="ISOLATED")
    position_mode: str = Field(default="ONE_WAY")
    entry_type: EntryType
    entry_price: float = Field(ge=0.0)
    stop_loss: float = Field(ge=0.0)
    take_profit: float = Field(ge=0.0)
    invalidation_price: float = Field(ge=0.0)

    # Parámetros de riesgo
    leverage: int = Field(ge=1, le=10)
    margin_usdt: float = Field(ge=0.0, le=10.0)
    estimated_notional_usdt: float = Field(ge=0.0)
    estimated_entry_fee_usdt: float = Field(ge=0.0)
    estimated_exit_fee_usdt: float = Field(ge=0.0)
    estimated_slippage_usdt: float = Field(ge=0.0)
    estimated_funding_usdt: float = Field(ge=0.0)
    net_risk_reward: float
    estimated_max_loss_usdt: float = Field(ge=0.0)
    liquidation_distance_percent_estimated: float = Field(ge=0.0)

    # Evaluación
    confidence: float = Field(ge=0.0, le=1.0)
    market_regime: str
    setup_name: str
    timeframes_used: list[str]

    # Contexto cuantitativo
    quant_signals: QuantSignalsSection
    decision_aggregator: DecisionAggregatorSection
    news_context: NewsContextSection
    position_management_plan: PositionManagementPlan

    # Narrativa
    decision_rationale_summary: str
    risk_notes: list[str] = Field(default_factory=list)

    # Ejecución
    execute: bool

    model_config = {"frozen": True}

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
        if v not in _ALLOWED_SYMBOLS:
            raise ValueError(f"Símbolo '{v}' no permitido. Válidos: {sorted(_ALLOWED_SYMBOLS)}")
        return v

    @field_validator("challenge_mode")
    @classmethod
    def challenge_mode_valid(cls, v: str) -> str:
        if v != CHALLENGE_MODE:
            raise ValueError(f"challenge_mode inválido: '{v}'. Debe ser '{CHALLENGE_MODE}'")
        return v

    @model_validator(mode="after")
    def no_operar_cannot_execute(self) -> ModelDecision:
        if self.decision == DecisionType.NO_OPERAR and self.execute:
            raise ValueError("execute debe ser False cuando decision=NO_OPERAR")
        return self

    @model_validator(mode="after")
    def execute_requires_sl_tp(self) -> ModelDecision:
        if self.execute and self.decision in (DecisionType.LONG, DecisionType.SHORT):
            if self.stop_loss <= 0:
                raise ValueError("stop_loss > 0 requerido cuando execute=True")
            if self.take_profit <= 0:
                raise ValueError("take_profit > 0 requerido cuando execute=True")
        return self
