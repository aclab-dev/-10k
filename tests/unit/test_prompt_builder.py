"""Tests unitarios para PromptBuilder y PromptContext (F7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from backend.core.config import Environment
from backend.decision_engine.prompt_builder import (
    PROMPT_VERSION,
    AccountContext,
    PromptBuilder,
    PromptContext,
)
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.market_regime.schemas import (
    FundingState,
    LiquidityState,
    MarketRegimeAssessment,
    OpenInterestState,
    PrimaryRegime,
    TrendAlignment,
    VolatilityState,
)
from backend.quant_signals.schemas import QuantSignalsPackage
from backend.volatility.schemas import VolatilityAssessmentPackage, VolatilityRegime

# ---------------------------------------------------------------------------
# Helpers de construcción
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _candle_data(price: str = "50000") -> CandleData:
    p = Decimal(price)
    return CandleData(
        open=p,
        high=p + 100,
        low=p - 100,
        close=p + 5,
        volume=Decimal("1000"),
        n_candles=1,
    )


def _snapshot(**overrides: object) -> MarketSnapshot:
    defaults: dict[str, object] = {
        "timestamp_utc": _now(),
        "exchange": Exchange.BINGX,
        "environment": Environment.PAPER,
        "symbol": "BTCUSDT",
        "last_price": Decimal("50005"),
        "bid": Decimal("50000"),
        "ask": Decimal("50010"),
        "spread_absolute": Decimal("10"),
        "spread_percent": Decimal("0.02"),
        "candles": Candles(
            tf_5m=_candle_data("50000"),
            tf_15m=_candle_data("50100"),
            tf_1h=_candle_data("49800"),
            tf_4h=_candle_data("49000"),
        ),
        "volume": Decimal("5000"),
        "account_balance_usdt": Decimal("100"),
        "open_positions_count": 0,
        "active_orders_count": 0,
        "latency_ms": 50,
        "exchange_server_time": _now(),
        "local_time": _now(),
        "clock_skew_ms": 0,
        "data_freshness_status": DataFreshnessStatus.FRESH,
        "coherence_status": CoherenceStatus.OK,
    }
    defaults.update(overrides)
    return MarketSnapshot(**defaults)  # type: ignore[arg-type]


def _quant_signals(**overrides: object) -> QuantSignalsPackage:
    defaults: dict[str, object] = {
        "snapshot_id": str(uuid.uuid4()),
        "timestamp_utc": _now(),
        "symbol": "BTCUSDT",
        "timeframes_used": ["5m", "15m", "1h", "4h"],
        "momentum_signal": 0.65,
        "mean_reversion_signal": -0.20,
        "breakout_signal": 0.50,
        "funding_signal": 0.10,
        "open_interest_signal": 0.30,
        "order_flow_imbalance_signal": 0.40,
        "liquidity_sweep_signal": 0.05,
        "signal_strength_score": 0.72,
        "signal_conflict_score": 0.25,
        "signal_confidence": 0.68,
    }
    defaults.update(overrides)
    return QuantSignalsPackage(**defaults)  # type: ignore[arg-type]


def _regime(**overrides: object) -> MarketRegimeAssessment:
    defaults: dict[str, object] = {
        "snapshot_id": str(uuid.uuid4()),
        "symbol": "BTCUSDT",
        "timestamp_utc": _now(),
        "primary_regime": PrimaryRegime.TRENDING,
        "regime_confidence": 0.78,
        "volatility_state": VolatilityState.NORMAL,
        "liquidity_state": LiquidityState.NORMAL,
        "funding_state": FundingState.NEUTRAL,
        "open_interest_state": OpenInterestState.RISING,
        "trend_alignment": TrendAlignment.BULLISH,
    }
    defaults.update(overrides)
    return MarketRegimeAssessment(**defaults)  # type: ignore[arg-type]


def _volatility(**overrides: object) -> VolatilityAssessmentPackage:
    defaults: dict[str, object] = {
        "snapshot_id": str(uuid.uuid4()),
        "timestamp_utc": _now(),
        "symbol": "BTCUSDT",
        "atr_5m": Decimal("80"),
        "atr_15m": Decimal("120"),
        "atr_1h": Decimal("200"),
        "atr_4h": Decimal("350"),
        "atr_percent": 0.40,
        "realized_vol": 0.004,
        "volatility_regime": VolatilityRegime.CONTRACTION,
        "volatility_score": 0.30,
        "liquidation_risk_score": 0.20,
        "leverage_cap": 8,
        "details": {},
    }
    defaults.update(overrides)
    return VolatilityAssessmentPackage(**defaults)  # type: ignore[arg-type]


def _account(**overrides: object) -> AccountContext:
    defaults: dict[str, object] = {
        "environment": "PAPER",
        "balance_usdt": 100.0,
        "open_positions_count": 0,
        "daily_drawdown_percent": 0.5,
        "max_leverage_for_environment": 10,
    }
    defaults.update(overrides)
    return AccountContext(**defaults)  # type: ignore[arg-type]


def _ctx(**overrides: object) -> PromptContext:
    defaults: dict[str, object] = {
        "snapshot": _snapshot(),
        "quant_signals": _quant_signals(),
        "regime": _regime(),
        "volatility": _volatility(),
        "account": _account(),
    }
    defaults.update(overrides)
    return PromptContext(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests de PromptBuilder
# ---------------------------------------------------------------------------


class TestPromptBuilderOutput:
    def setup_method(self) -> None:
        self.builder = PromptBuilder()
        self.ctx = _ctx()
        self.system, self.user = self.builder.build(self.ctx)

    def test_build_returns_tuple_of_strings(self) -> None:
        assert isinstance(self.system, str)
        assert isinstance(self.user, str)

    def test_system_is_not_empty(self) -> None:
        assert len(self.system) > 100

    def test_user_is_not_empty(self) -> None:
        assert len(self.user) > 100

    def test_system_contains_prompt_version(self) -> None:
        assert PROMPT_VERSION in self.system

    def test_system_contains_challenge_mode(self) -> None:
        assert "AUTONOMOUS_FUTURES_GPT55_QUANT_CONTROLLED_RISK" in self.system

    def test_system_contains_no_operar(self) -> None:
        assert "NO_OPERAR" in self.system

    def test_system_contains_json_schema_fields(self) -> None:
        for field in ("decision", "symbol", "stop_loss", "take_profit", "leverage", "confidence"):
            assert field in self.system

    def test_system_instructs_evaluator_not_edge(self) -> None:
        assert "evaluador" in self.system.lower() or "Evaluador" in self.system

    def test_system_prohibits_extra_text(self) -> None:
        assert "Solo JSON" in self.system or "solo el JSON" in self.system.lower()

    def test_system_enforces_margin_limit(self) -> None:
        assert "10" in self.system and "margin" in self.system.lower()


class TestUserMessageContent:
    def setup_method(self) -> None:
        self.builder = PromptBuilder()
        self.ctx = _ctx()
        _, self.user = self.builder.build(self.ctx)

    def test_user_contains_symbol(self) -> None:
        assert "BTCUSDT" in self.user

    def test_user_contains_snapshot_price(self) -> None:
        assert "50005" in self.user

    def test_user_contains_quant_signal_values(self) -> None:
        assert "+0.6500" in self.user or "0.65" in self.user

    def test_user_contains_regime_info(self) -> None:
        assert "TRENDING" in self.user

    def test_user_contains_volatility_regime(self) -> None:
        assert "CONTRACTION" in self.user

    def test_user_contains_leverage_cap(self) -> None:
        assert "8" in self.user

    def test_user_contains_prompt_version(self) -> None:
        assert PROMPT_VERSION in self.user

    def test_user_contains_account_environment(self) -> None:
        assert "PAPER" in self.user

    def test_user_contains_all_signal_ids(self) -> None:
        assert self.ctx.snapshot.snapshot_id in self.user
        assert self.ctx.quant_signals.signal_id in self.user
        assert self.ctx.regime.regime_id in self.user
        assert self.ctx.volatility.assessment_id in self.user

    def test_user_contains_regime_confidence(self) -> None:
        assert "0.78" in self.user

    def test_user_contains_funding_state(self) -> None:
        assert "NEUTRAL" in self.user


class TestUserMessageSections:
    def setup_method(self) -> None:
        builder = PromptBuilder()
        _, self.user = builder.build(_ctx())

    def test_snapshot_section_present(self) -> None:
        assert "## Snapshot de mercado" in self.user

    def test_account_section_present(self) -> None:
        assert "## Estado de cuenta" in self.user

    def test_quant_signals_section_present(self) -> None:
        assert "## Señales cuantitativas" in self.user

    def test_regime_section_present(self) -> None:
        assert "## Régimen de mercado" in self.user

    def test_volatility_section_present(self) -> None:
        assert "## Volatilidad" in self.user

    def test_instructions_section_present(self) -> None:
        assert "## Instrucciones" in self.user


class TestPromptVersioning:
    def test_prompt_version_constant_is_semver_like(self) -> None:
        parts = PROMPT_VERSION.split(".")
        assert len(parts) >= 2
        assert all(p.isdigit() for p in parts)

    def test_prompt_context_carries_version(self) -> None:
        ctx = _ctx()
        assert ctx.prompt_version == PROMPT_VERSION

    def test_custom_version_propagates_to_user_message(self) -> None:
        ctx = PromptContext(
            snapshot=_snapshot(),
            quant_signals=_quant_signals(),
            regime=_regime(),
            volatility=_volatility(),
            account=_account(),
            prompt_version="2.0",
        )
        _, user = PromptBuilder().build(ctx)
        assert "2.0" in user

    def test_system_prompt_is_class_attribute(self) -> None:
        builder = PromptBuilder()
        assert builder.SYSTEM_PROMPT is PromptBuilder.SYSTEM_PROMPT


class TestNASignals:
    """Señales opcionales None se muestran como N/A en el prompt."""

    def test_none_signals_render_as_na(self) -> None:
        qs = _quant_signals(
            momentum_signal=None,
            liquidity_sweep_signal=None,
            signal_strength_score=None,
        )
        ctx = _ctx(quant_signals=qs)
        _, user = PromptBuilder().build(ctx)
        assert "N/A" in user

    def test_regime_notes_included_when_present(self) -> None:
        regime = _regime(notes=["Alta dispersion entre timeframes"])
        ctx = _ctx(regime=regime)
        _, user = PromptBuilder().build(ctx)
        assert "Alta dispersion" in user
