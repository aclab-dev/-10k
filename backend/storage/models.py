"""Modelos SQLAlchemy — 27 tablas del Anexo B (PDF §8.2).

Convenciones:
- id: UUID generado en Python (server_default=gen_random_uuid() en la migración).
- created_at/updated_at: timestamps UTC con timezone (TIMESTAMPTZ en Postgres).
- PgJSON para payloads variables; columnas explícitas para los campos que se consultan.
- FKs con ondelete="SET NULL" en referencias opcionales; "CASCADE" en relaciones
  padre-hijo donde borrar el padre invalida el hijo.
- Numeric(20, 8) para valores monetarios y precios; Float para scores/ratios/porcentajes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.storage.database import Base, PgJSON


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# 1. bot_runs
# ---------------------------------------------------------------------------
class BotRun(Base):
    __tablename__ = "bot_runs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)  # PAPER/TESTNET/LIVE
    app_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(PgJSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # relationships
    bot_states: Mapped[list[BotState]] = relationship(back_populates="bot_run")
    account_states: Mapped[list[AccountState]] = relationship(back_populates="bot_run")
    market_snapshots: Mapped[list[MarketSnapshot]] = relationship(back_populates="bot_run")
    quant_signals: Mapped[list[QuantSignal]] = relationship(back_populates="bot_run")
    market_regimes: Mapped[list[MarketRegime]] = relationship(back_populates="bot_run")
    volatility_assessments: Mapped[list[VolatilityAssessment]] = relationship(
        back_populates="bot_run"
    )
    feature_packages: Mapped[list[FeaturePackage]] = relationship(back_populates="bot_run")
    model_requests: Mapped[list[ModelRequest]] = relationship(back_populates="bot_run")
    decisions: Mapped[list[Decision]] = relationship(back_populates="bot_run")
    decision_aggregations: Mapped[list[DecisionAggregation]] = relationship(
        back_populates="bot_run"
    )
    risk_validations: Mapped[list[RiskValidation]] = relationship(back_populates="bot_run")
    trades: Mapped[list[Trade]] = relationship(back_populates="bot_run")
    orders: Mapped[list[Order]] = relationship(back_populates="bot_run")
    positions: Mapped[list[Position]] = relationship(back_populates="bot_run")
    strategy_performances: Mapped[list[StrategyPerformance]] = relationship(
        back_populates="bot_run"
    )
    historical_replay_runs: Mapped[list[HistoricalReplayRun]] = relationship(
        back_populates="bot_run"
    )
    backtest_runs: Mapped[list[BacktestRun]] = relationship(back_populates="bot_run")
    news_contexts: Mapped[list[NewsContext]] = relationship(back_populates="bot_run")
    token_usages: Mapped[list[TokenUsage]] = relationship(back_populates="bot_run")
    system_events: Mapped[list[SystemEvent]] = relationship(back_populates="bot_run")
    errors: Mapped[list[ErrorRecord]] = relationship(back_populates="bot_run")
    kill_switch_events: Mapped[list[KillSwitchEvent]] = relationship(back_populates="bot_run")


# ---------------------------------------------------------------------------
# 2. bot_state
# ---------------------------------------------------------------------------
class BotState(Base):
    __tablename__ = "bot_state"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    bot_run: Mapped[BotRun] = relationship(back_populates="bot_states")


# ---------------------------------------------------------------------------
# 3. accounts_state
# ---------------------------------------------------------------------------
class AccountState(Base):
    __tablename__ = "accounts_state"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    balance_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    equity_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    margin_used_usdt: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0")
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0")
    )
    realized_pnl_session: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0")
    )
    drawdown_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exposure_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)

    bot_run: Mapped[BotRun] = relationship(back_populates="account_states")


# ---------------------------------------------------------------------------
# 4. market_snapshots
# ---------------------------------------------------------------------------
class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    spread: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    extra: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="market_snapshots")
    quant_signals: Mapped[list[QuantSignal]] = relationship(back_populates="market_snapshot")
    market_regimes: Mapped[list[MarketRegime]] = relationship(back_populates="market_snapshot")
    volatility_assessments: Mapped[list[VolatilityAssessment]] = relationship(
        back_populates="market_snapshot"
    )
    feature_packages: Mapped[list[FeaturePackage]] = relationship(back_populates="market_snapshot")


# ---------------------------------------------------------------------------
# 5. quant_signals
# ---------------------------------------------------------------------------
class QuantSignal(Base):
    __tablename__ = "quant_signals"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    market_snapshot_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("market_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    momentum_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    mean_reversion_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    breakout_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_sweep_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_flow_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_interest_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_signals: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="quant_signals")
    market_snapshot: Mapped[MarketSnapshot | None] = relationship(back_populates="quant_signals")


# ---------------------------------------------------------------------------
# 6. market_regimes
# ---------------------------------------------------------------------------
class MarketRegime(Base):
    __tablename__ = "market_regimes"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    market_snapshot_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("market_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    regime_details: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="market_regimes")
    market_snapshot: Mapped[MarketSnapshot | None] = relationship(back_populates="market_regimes")


# ---------------------------------------------------------------------------
# 7. volatility_assessments
# ---------------------------------------------------------------------------
class VolatilityAssessment(Base):
    __tablename__ = "volatility_assessments"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    market_snapshot_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("market_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    atr: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    atr_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    leverage_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    liquidation_risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="volatility_assessments")
    market_snapshot: Mapped[MarketSnapshot | None] = relationship(
        back_populates="volatility_assessments"
    )


# ---------------------------------------------------------------------------
# 8. feature_packages
# ---------------------------------------------------------------------------
class FeaturePackage(Base):
    __tablename__ = "feature_packages"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    market_snapshot_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("market_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(PgJSON, nullable=False)
    features_hash: Mapped[str] = mapped_column("hash", String(64), nullable=False, unique=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="feature_packages")
    market_snapshot: Mapped[MarketSnapshot | None] = relationship(back_populates="feature_packages")
    model_requests: Mapped[list[ModelRequest]] = relationship(back_populates="feature_package")


# ---------------------------------------------------------------------------
# 9. model_requests
# ---------------------------------------------------------------------------
class ModelRequest(Base):
    __tablename__ = "model_requests"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    feature_package_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("feature_packages.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context: Mapped[dict[str, Any]] = mapped_column(PgJSON, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    bot_run: Mapped[BotRun] = relationship(back_populates="model_requests")
    feature_package: Mapped[FeaturePackage | None] = relationship(back_populates="model_requests")
    model_response: Mapped[ModelResponse | None] = relationship(
        back_populates="model_request", uselist=False
    )
    token_usage: Mapped[TokenUsage | None] = relationship(
        back_populates="model_request", uselist=False
    )


# ---------------------------------------------------------------------------
# 10. model_responses
# ---------------------------------------------------------------------------
class ModelResponse(Base):
    __tablename__ = "model_responses"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    model_request_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("model_requests.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_response: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_response: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_valid_schema: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    model_request: Mapped[ModelRequest] = relationship(back_populates="model_response")
    decision: Mapped[Decision | None] = relationship(back_populates="model_response", uselist=False)


# ---------------------------------------------------------------------------
# 11. decisions
# ---------------------------------------------------------------------------
class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    model_response_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("model_responses.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # OPEN/CLOSE/NO_OPERAR
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)  # LONG/SHORT
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    margin_usdt: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_decision: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="decisions")
    model_response: Mapped[ModelResponse | None] = relationship(back_populates="decision")
    decision_aggregation: Mapped[DecisionAggregation | None] = relationship(
        back_populates="decision", uselist=False
    )


# ---------------------------------------------------------------------------
# 12. decision_aggregations
# ---------------------------------------------------------------------------
class DecisionAggregation(Base):
    __tablename__ = "decision_aggregations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    decision_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quant_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    gpt_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    volatility_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    aggregated_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_action: Mapped[str] = mapped_column(String(16), nullable=False)
    reasons: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="decision_aggregations")
    decision: Mapped[Decision | None] = relationship(back_populates="decision_aggregation")
    risk_validation: Mapped[RiskValidation | None] = relationship(
        back_populates="decision_aggregation", uselist=False
    )


# ---------------------------------------------------------------------------
# 13. risk_validations
# ---------------------------------------------------------------------------
class RiskValidation(Base):
    __tablename__ = "risk_validations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    decision_aggregation_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("decision_aggregations.id", ondelete="SET NULL"),
        nullable=True,
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)  # APPROVE/ADJUST_DOWN/BLOCK
    original_margin: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    original_leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adjusted_margin: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    adjusted_leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasons: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)
    daily_loss_at_check: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    total_loss_at_check: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="risk_validations")
    decision_aggregation: Mapped[DecisionAggregation | None] = relationship(
        back_populates="risk_validation"
    )
    trade: Mapped[Trade | None] = relationship(back_populates="risk_validation", uselist=False)


# ---------------------------------------------------------------------------
# 14. trades
# ---------------------------------------------------------------------------
class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    risk_validation_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("risk_validations.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG/SHORT
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    margin_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    gross_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    net_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    funding_paid: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="trades")
    risk_validation: Mapped[RiskValidation | None] = relationship(back_populates="trade")
    orders: Mapped[list[Order]] = relationship(back_populates="trade")
    position: Mapped[Position | None] = relationship(back_populates="trade", uselist=False)


# ---------------------------------------------------------------------------
# 15. orders
# ---------------------------------------------------------------------------
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    trade_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )
    client_order_id: Mapped[str] = mapped_column(String(36), nullable=False)
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)  # MARKET/LIMIT/STOP
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY/SELL
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),)

    bot_run: Mapped[BotRun] = relationship(back_populates="orders")
    trade: Mapped[Trade | None] = relationship(back_populates="orders")


# ---------------------------------------------------------------------------
# 16. positions
# ---------------------------------------------------------------------------
class Position(Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    trade_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    current_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    margin_usdt: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    trailing_stop_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    break_even_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    # onupdate solo aplica a nivel ORM; UPDATEs SQL directos no actualizan este campo
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    bot_run: Mapped[BotRun] = relationship(back_populates="positions")
    trade: Mapped[Trade | None] = relationship(back_populates="position")
    events: Mapped[list[PositionEvent]] = relationship(back_populates="position")


# ---------------------------------------------------------------------------
# 17. position_events
# ---------------------------------------------------------------------------
class PositionEvent(Base):
    __tablename__ = "position_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    position_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("positions.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # SL_UPDATE / TP_UPDATE / TRAILING / PARTIAL_CLOSE / CLOSE / INVALIDATION
    old_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    new_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    position: Mapped[Position] = relationship(back_populates="events")


# ---------------------------------------------------------------------------
# 18. strategy_performance
# ---------------------------------------------------------------------------
class StrategyPerformance(Base):
    __tablename__ = "strategy_performance"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    regime: Mapped[str | None] = mapped_column(String(32), nullable=True)
    setup_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    total_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="strategy_performances")


# ---------------------------------------------------------------------------
# 19. historical_replay_runs
# ---------------------------------------------------------------------------
class HistoricalReplayRun(Base):
    __tablename__ = "historical_replay_runs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(PgJSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    total_snapshots: Mapped[int | None] = mapped_column(Integer, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="historical_replay_runs")
    snapshots: Mapped[list[HistoricalReplaySnapshot]] = relationship(back_populates="replay_run")


# ---------------------------------------------------------------------------
# 20. historical_replay_snapshots
# ---------------------------------------------------------------------------
class HistoricalReplaySnapshot(Base):
    __tablename__ = "historical_replay_snapshots"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    replay_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("historical_replay_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_num: Mapped[int] = mapped_column(BigInteger, nullable=False)
    market_snapshot: Mapped[dict[str, Any]] = mapped_column(PgJSON, nullable=False)
    decision: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)
    risk_validation: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)
    comparison_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    replay_run: Mapped[HistoricalReplayRun] = relationship(back_populates="snapshots")


# ---------------------------------------------------------------------------
# 21. backtest_runs
# ---------------------------------------------------------------------------
class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(PgJSON, nullable=False)
    fee_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    slippage_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    funding_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")

    bot_run: Mapped[BotRun] = relationship(back_populates="backtest_runs")
    results: Mapped[list[BacktestResult]] = relationship(back_populates="backtest_run")


# ---------------------------------------------------------------------------
# 22. backtest_results
# ---------------------------------------------------------------------------
class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    backtest_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    gross_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    total_fees: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    total_funding: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    sortino_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    expectancy_usdt: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_trade_duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_trade_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    worst_trade_pnl: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    backtest_run: Mapped[BacktestRun] = relationship(back_populates="results")


# ---------------------------------------------------------------------------
# 23. news_context
# ---------------------------------------------------------------------------
class NewsContext(Base):
    __tablename__ = "news_context"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )  # POSITIVE/NEGATIVE/NEUTRAL
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_data: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="news_contexts")


# ---------------------------------------------------------------------------
# 24. token_usage
# ---------------------------------------------------------------------------
class TokenUsage(Base):
    __tablename__ = "token_usage"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    model_request_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False), ForeignKey("model_requests.id", ondelete="SET NULL"), nullable=True
    )
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="token_usages")
    model_request: Mapped[ModelRequest | None] = relationship(back_populates="token_usage")


# ---------------------------------------------------------------------------
# 25. system_events
# ---------------------------------------------------------------------------
class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="system_events")


# ---------------------------------------------------------------------------
# 26. errors
# ---------------------------------------------------------------------------
class ErrorRecord(Base):
    __tablename__ = "errors"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    module: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(PgJSON, nullable=True)
    recovered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    bot_run: Mapped[BotRun] = relationship(back_populates="errors")


# ---------------------------------------------------------------------------
# 27. kill_switch_events
# ---------------------------------------------------------------------------
class KillSwitchEvent(Base):
    __tablename__ = "kill_switch_events"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    bot_run_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bot_runs.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False)
    state_before: Mapped[str] = mapped_column(String(32), nullable=False)
    action_taken: Mapped[str] = mapped_column(String(64), nullable=False)
    positions_closed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_cancelled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    bot_run: Mapped[BotRun] = relationship(back_populates="kill_switch_events")


# ---------------------------------------------------------------------------
# 28. strategy_setups  (F12 — Strategy/Setup Registry)
# ---------------------------------------------------------------------------
class StrategySetup(Base):
    __tablename__ = "strategy_setups"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(96), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(PgJSON, nullable=False, default=dict)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", PgJSON, nullable=False, default=dict
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    __table_args__ = (UniqueConstraint("name", name="uq_strategy_setups_name"),)


# ---------------------------------------------------------------------------
# Auth throttling (F15) — operativas del dashboard, fuera del Anexo B
#
# Viven en Postgres y no en memoria porque el contador tiene que ser compartido
# entre workers y sobrevivir a un reinicio: si no, se evade levantando procesos o
# esperando a que el proceso recicle. No usan `system_events` porque esa tabla
# exige `bot_run_id`, y un intento de login no pertenece a ninguna corrida del bot.
# ---------------------------------------------------------------------------


class LoginScope(StrEnum):
    """Dimensión por la que se cuentan los fallos de login."""

    USERNAME = "USERNAME"
    IP = "IP"


class LoginAttempt(Base):
    """Un intento de login fallido, para el conteo por ventana deslizante.

    Solo se persisten los fallos: los logins exitosos no aportan al conteo y
    guardarlos convertiría la tabla en un log de sesiones que nadie pidió.
    """

    __tablename__ = "login_attempts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # LoginScope
    identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        Index("ix_login_attempts_scope_identifier_timestamp", "scope", "identifier", "timestamp"),
        # La purga filtra solo por timestamp, y el índice compuesto no le sirve
        # porque timestamp es su última columna.
        Index("ix_login_attempts_timestamp", "timestamp"),
    )


class LoginLockout(Base):
    """Bloqueo activo (o vencido) de un usuario o IP.

    `lockout_count` sobrevive al vencimiento del bloqueo a propósito: es lo que
    hace crecer el backoff ante reincidencia. Se resetea solo con un login exitoso.
    """

    __tablename__ = "login_lockouts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)  # LoginScope
    identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lockout_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )

    __table_args__ = (
        UniqueConstraint("scope", "identifier", name="uq_login_lockouts_scope_identifier"),
    )


__all__ = [
    "Base",
    "BotRun",
    "BotState",
    "AccountState",
    "MarketSnapshot",
    "QuantSignal",
    "MarketRegime",
    "VolatilityAssessment",
    "FeaturePackage",
    "ModelRequest",
    "ModelResponse",
    "Decision",
    "DecisionAggregation",
    "RiskValidation",
    "Trade",
    "Order",
    "Position",
    "PositionEvent",
    "StrategyPerformance",
    "HistoricalReplayRun",
    "HistoricalReplaySnapshot",
    "BacktestRun",
    "BacktestResult",
    "NewsContext",
    "TokenUsage",
    "SystemEvent",
    "ErrorRecord",
    "KillSwitchEvent",
    "StrategySetup",
    "LoginScope",
    "LoginAttempt",
    "LoginLockout",
]
