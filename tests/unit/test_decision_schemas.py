"""Tests unitarios para ModelDecision y sus sub-schemas (sección 3.8)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from backend.decision_engine.schemas import (
    CHALLENGE_MODE,
    DECISION_SCHEMA_VERSION,
    BreakoutInterpretation,
    DecisionAggregatorSection,
    DecisionType,
    EntryType,
    FundingInterpretation,
    LiquiditySweepInterpretation,
    MeanReversionInterpretation,
    ModelDecision,
    MomentumInterpretation,
    NewsContextSection,
    NewsImpact,
    OpenInterestInterpretation,
    OrderFlowInterpretation,
    PositionManagementPlan,
    QuantSignalsSection,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quant_signals_section(**overrides: object) -> QuantSignalsSection:
    defaults: dict[str, object] = {
        "momentum": MomentumInterpretation.BULLISH,
        "mean_reversion": MeanReversionInterpretation.LONG_BIAS,
        "breakout_detection": BreakoutInterpretation.CONFIRMED,
        "funding_analysis": FundingInterpretation.SUPPORTS_TRADE,
        "open_interest_analysis": OpenInterestInterpretation.RISING_WITH_PRICE,
        "order_flow_imbalance": OrderFlowInterpretation.BUY_PRESSURE,
        "liquidity_sweep": LiquiditySweepInterpretation.NONE,
    }
    defaults.update(overrides)
    return QuantSignalsSection(**defaults)  # type: ignore[arg-type]


def _aggregator(**overrides: object) -> DecisionAggregatorSection:
    defaults: dict[str, object] = {
        "quant_score": 0.75,
        "gpt_context_score": 0.70,
        "risk_quality_score": 0.80,
        "final_trade_quality_score": 0.75,
    }
    defaults.update(overrides)
    return DecisionAggregatorSection(**defaults)  # type: ignore[arg-type]


def _news(**overrides: object) -> NewsContextSection:
    defaults: dict[str, object] = {
        "used": False,
        "impact": NewsImpact.NEUTRAL,
        "summary": "",
    }
    defaults.update(overrides)
    return NewsContextSection(**defaults)  # type: ignore[arg-type]


def _position_plan(**overrides: object) -> PositionManagementPlan:
    defaults: dict[str, object] = {
        "use_trailing_stop": True,
        "move_to_break_even": True,
        "partial_close_plan": "",
        "max_time_in_trade_minutes": 240,
    }
    defaults.update(overrides)
    return PositionManagementPlan(**defaults)  # type: ignore[arg-type]


def _decision(**overrides: object) -> ModelDecision:
    """Construye un ModelDecision NO_OPERAR válido por defecto."""
    defaults: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": "2026-05-25T10:00:00Z",
        "decision": DecisionType.NO_OPERAR,
        "symbol": "BTCUSDT",
        "entry_type": EntryType.NO_ENTRY,
        "entry_price": 0.0,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "invalidation_price": 0.0,
        "leverage": 1,
        "margin_usdt": 0.0,
        "estimated_notional_usdt": 0.0,
        "estimated_entry_fee_usdt": 0.0,
        "estimated_exit_fee_usdt": 0.0,
        "estimated_slippage_usdt": 0.0,
        "estimated_funding_usdt": 0.0,
        "net_risk_reward": 0.0,
        "estimated_max_loss_usdt": 0.0,
        "liquidation_distance_percent_estimated": 0.0,
        "confidence": 0.0,
        "market_regime": "UNCLEAR",
        "setup_name": "no_setup",
        "timeframes_used": ["5m", "15m", "1h", "4h"],
        "quant_signals": _quant_signals_section(),
        "decision_aggregator": _aggregator(),
        "news_context": _news(),
        "position_management_plan": _position_plan(),
        "decision_rationale_summary": "No hay edge suficiente.",
        "execute": False,
    }
    defaults.update(overrides)
    return ModelDecision(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Construcción básica
# ---------------------------------------------------------------------------


class TestModelDecisionConstruction:
    def test_no_operar_valid(self) -> None:
        d = _decision()
        assert d.decision == DecisionType.NO_OPERAR
        assert not d.execute

    def test_decision_id_auto_generated_as_uuid(self) -> None:
        d = _decision()
        assert uuid.UUID(d.decision_id)

    def test_challenge_mode_defaults_correctly(self) -> None:
        d = _decision()
        assert d.challenge_mode == CHALLENGE_MODE

    def test_schema_version_defaults_correctly(self) -> None:
        d = _decision()
        assert d.schema_version == DECISION_SCHEMA_VERSION

    def test_long_without_execute_valid(self) -> None:
        d = _decision(
            decision=DecisionType.LONG,
            entry_type=EntryType.MARKET,
            entry_price=50000.0,
            stop_loss=0.0,
            take_profit=0.0,
            margin_usdt=5.0,
            leverage=3,
            execute=False,
        )
        assert d.decision == DecisionType.LONG
        assert not d.execute

    def test_long_with_execute_and_sl_tp_valid(self) -> None:
        d = _decision(
            decision=DecisionType.LONG,
            entry_type=EntryType.MARKET,
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            invalidation_price=48500.0,
            leverage=3,
            margin_usdt=5.0,
            estimated_notional_usdt=15.0,
            estimated_entry_fee_usdt=0.015,
            estimated_exit_fee_usdt=0.015,
            estimated_slippage_usdt=0.05,
            estimated_funding_usdt=0.01,
            net_risk_reward=2.0,
            estimated_max_loss_usdt=5.0,
            liquidation_distance_percent_estimated=8.5,
            confidence=0.80,
            market_regime="TRENDING",
            setup_name="breakout_pullback_v1",
            execute=True,
        )
        assert d.execute
        assert d.stop_loss == 49000.0

    def test_model_is_frozen(self) -> None:
        d = _decision()
        with pytest.raises(ValidationError):
            d.execute = True


# ---------------------------------------------------------------------------
# Validaciones de campo
# ---------------------------------------------------------------------------


class TestModelDecisionValidations:
    def test_invalid_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no permitido"):
            _decision(symbol="DOGEUSDT")

    def test_invalid_challenge_mode_rejected(self) -> None:
        with pytest.raises(ValidationError, match="challenge_mode inválido"):
            _decision(challenge_mode="WRONG_MODE")

    def test_invalid_uuid_rejected(self) -> None:
        with pytest.raises(ValidationError, match="UUID"):
            _decision(decision_id="not-a-uuid")

    def test_margin_above_10_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(margin_usdt=10.01)

    def test_confidence_above_1_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(confidence=1.01)

    def test_confidence_below_0_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(confidence=-0.01)

    def test_leverage_above_10_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(leverage=11)

    def test_leverage_below_1_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(leverage=0)

    def test_negative_entry_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(entry_price=-1.0)

    def test_negative_stop_loss_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _decision(stop_loss=-1.0)


# ---------------------------------------------------------------------------
# Reglas de coherencia entre campos (model_validators)
# ---------------------------------------------------------------------------


class TestModelDecisionCoherence:
    def test_no_operar_with_execute_true_rejected(self) -> None:
        with pytest.raises(ValidationError, match="execute debe ser False"):
            _decision(decision=DecisionType.NO_OPERAR, execute=True)

    def test_short_execute_true_without_stop_loss_rejected(self) -> None:
        with pytest.raises(ValidationError, match="stop_loss > 0"):
            _decision(
                decision=DecisionType.SHORT,
                entry_type=EntryType.MARKET,
                entry_price=50000.0,
                stop_loss=0.0,
                take_profit=48000.0,
                margin_usdt=5.0,
                leverage=3,
                execute=True,
            )

    def test_long_execute_true_without_take_profit_rejected(self) -> None:
        with pytest.raises(ValidationError, match="take_profit > 0"):
            _decision(
                decision=DecisionType.LONG,
                entry_type=EntryType.MARKET,
                entry_price=50000.0,
                stop_loss=49000.0,
                take_profit=0.0,
                margin_usdt=5.0,
                leverage=3,
                execute=True,
            )

    def test_long_execute_false_allows_zero_sl_tp(self) -> None:
        d = _decision(
            decision=DecisionType.LONG,
            entry_type=EntryType.MARKET,
            entry_price=50000.0,
            stop_loss=0.0,
            take_profit=0.0,
            execute=False,
        )
        assert d.decision == DecisionType.LONG

    def test_execute_true_with_low_rr_rejected(self) -> None:
        with pytest.raises(ValidationError, match="net_risk_reward"):
            _decision(
                decision=DecisionType.LONG,
                entry_type=EntryType.MARKET,
                entry_price=50000.0,
                stop_loss=49000.0,
                take_profit=51500.0,
                net_risk_reward=1.4,
                margin_usdt=5.0,
                leverage=3,
                execute=True,
            )

    def test_execute_false_allows_low_rr(self) -> None:
        """Sin execute no se exige RR mínimo — es solo análisis."""
        d = _decision(
            decision=DecisionType.LONG,
            entry_type=EntryType.MARKET,
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=51500.0,
            net_risk_reward=0.5,
            execute=False,
        )
        assert d.net_risk_reward == 0.5

    def test_negative_funding_usdt_accepted(self) -> None:
        """Funding negativo es válido: significa que el trader recibe funding."""
        d = _decision(estimated_funding_usdt=-0.05)
        assert d.estimated_funding_usdt == -0.05


# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------


class TestSubSchemas:
    def test_aggregator_scores_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _aggregator(quant_score=1.01)

    def test_position_plan_negative_minutes_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _position_plan(max_time_in_trade_minutes=-1)

    def test_quant_signals_enum_values_stored(self) -> None:
        qs = _quant_signals_section()
        assert qs.momentum == MomentumInterpretation.BULLISH
        assert qs.liquidity_sweep == LiquiditySweepInterpretation.NONE

    def test_news_context_all_impacts_accepted(self) -> None:
        for impact in NewsImpact:
            n = _news(impact=impact)
            assert n.impact == impact
