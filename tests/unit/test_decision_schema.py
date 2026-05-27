"""Tests unitarios — ModelDecision y schema_guard (sección 3.8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.core.config import Environment
from backend.decision_engine.schema_guard import validate_gpt_response
from backend.decision_engine.schemas import (
    CHALLENGE_MODE,
    DECISION_SCHEMA_VERSION,
    BreakoutInterpretation,
    DecisionAggregatorSection,
    DecisionType,
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


def _now() -> datetime:
    return datetime.now(UTC)


def _quant_signals(
    momentum: MomentumInterpretation = MomentumInterpretation.BULLISH,
) -> QuantSignalsSection:
    return QuantSignalsSection(
        momentum=momentum,
        mean_reversion=MeanReversionInterpretation.NEUTRAL,
        breakout_detection=BreakoutInterpretation.CONFIRMED,
        funding_analysis=FundingInterpretation.SUPPORTS_TRADE,
        open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
        order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
        liquidity_sweep=LiquiditySweepInterpretation.NONE,
    )


def _aggregator() -> DecisionAggregatorSection:
    return DecisionAggregatorSection(
        quant_score=0.8,
        gpt_context_score=0.85,
        risk_quality_score=0.75,
        final_trade_quality_score=0.80,
    )


def _news() -> NewsContextSection:
    return NewsContextSection(
        used=False,
        impact=NewsImpact.NEUTRAL,
        summary="No relevant news.",
    )


def _position_plan() -> PositionManagementPlan:
    return PositionManagementPlan(
        use_trailing_stop=True,
        move_to_break_even=True,
        partial_close_plan="none",
        max_time_in_trade_minutes=120,
    )


def _long_dict(**overrides: object) -> dict[str, object]:
    """Dict válido para LONG ejecutable en PAPER."""
    base: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": _now().isoformat(),
        "decision": "LONG",
        "symbol": "BTCUSDT",
        "entry_type": "MARKET",
        "entry_price": 95000.0,
        "stop_loss": 90000.0,
        "take_profit": 105000.0,
        "invalidation_price": 89000.0,
        "leverage": 5,
        "margin_usdt": 5.0,
        "estimated_notional_usdt": 25.0,
        "estimated_entry_fee_usdt": 0.025,
        "estimated_exit_fee_usdt": 0.025,
        "estimated_slippage_usdt": 0.05,
        "estimated_funding_usdt": -0.01,
        "net_risk_reward": 2.0,
        "estimated_max_loss_usdt": 5.0,
        "liquidation_distance_percent_estimated": 18.0,
        "confidence": 0.82,
        "market_regime": "TRENDING",
        "setup_name": "momentum_breakout",
        "timeframes_used": ["15m", "1h", "4h"],
        "quant_signals": _quant_signals().model_dump(),
        "decision_aggregator": _aggregator().model_dump(),
        "news_context": _news().model_dump(),
        "position_management_plan": _position_plan().model_dump(),
        "decision_rationale_summary": "Strong momentum with breakout confirmed on 1h.",
        "execute": True,
    }
    base.update(overrides)
    return base


def _build(**overrides: object) -> ModelDecision:
    return ModelDecision.model_validate(_long_dict(**overrides))


# ---------------------------------------------------------------------------
# Construcción básica
# ---------------------------------------------------------------------------


class TestModelDecisionConstruction:
    def test_valid_long_accepted(self) -> None:
        d = _build()
        assert d.decision == DecisionType.LONG
        assert d.symbol == "BTCUSDT"
        assert d.schema_version == DECISION_SCHEMA_VERSION
        assert d.challenge_mode == CHALLENGE_MODE

    def test_decision_id_auto_generated(self) -> None:
        d = _build()
        assert uuid.UUID(d.decision_id)

    def test_two_instances_have_different_ids(self) -> None:
        assert _build().decision_id != _build().decision_id

    def test_model_is_frozen(self) -> None:
        d = _build()
        # Pydantic v2 frozen lanza ValidationError (no AttributeError como dataclasses frozen)
        with pytest.raises(ValidationError):
            d.symbol = "ETHUSDT"

    def test_valid_short_accepted(self) -> None:
        d = _build(
            decision="SHORT",
            stop_loss=100000.0,
            take_profit=85000.0,
        )
        assert d.decision == DecisionType.SHORT

    def test_valid_no_operar_accepted(self) -> None:
        d = ModelDecision.model_validate(
            _long_dict(
                decision="NO_OPERAR",
                entry_type="NO_ENTRY",
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                invalidation_price=0.0,
                execute=False,
            )
        )
        assert d.decision == DecisionType.NO_OPERAR
        assert d.execute is False

    def test_defaults_applied(self) -> None:
        d = _build()
        assert d.market == "USDT_M_FUTURES"
        assert d.exchange_preference == "BINGX"


# ---------------------------------------------------------------------------
# Validación de symbol
# ---------------------------------------------------------------------------


class TestSymbolValidation:
    @pytest.mark.parametrize("sym", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"])
    def test_all_allowed_symbols_accepted(self, sym: str) -> None:
        assert _build(symbol=sym).symbol == sym

    def test_unknown_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no permitido"):
            _build(symbol="DOGEUSDT")

    def test_lowercase_symbol_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(symbol="btcusdt")


# ---------------------------------------------------------------------------
# challenge_mode
# ---------------------------------------------------------------------------


class TestChallengeMode:
    def test_correct_challenge_mode_accepted(self) -> None:
        d = _build(challenge_mode=CHALLENGE_MODE)
        assert d.challenge_mode == CHALLENGE_MODE

    def test_wrong_challenge_mode_rejected(self) -> None:
        with pytest.raises(ValidationError, match="challenge_mode inválido"):
            _build(challenge_mode="WRONG_MODE")


# ---------------------------------------------------------------------------
# NO_OPERAR no puede ejecutar
# ---------------------------------------------------------------------------


class TestNoOperarCannotExecute:
    def test_no_operar_execute_false_accepted(self) -> None:
        d = ModelDecision.model_validate(
            _long_dict(
                decision="NO_OPERAR",
                entry_type="NO_ENTRY",
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                invalidation_price=0.0,
                execute=False,
            )
        )
        assert not d.execute

    def test_no_operar_execute_true_rejected(self) -> None:
        with pytest.raises(ValidationError, match="execute debe ser False"):
            ModelDecision.model_validate(
                _long_dict(
                    decision="NO_OPERAR",
                    entry_type="NO_ENTRY",
                    entry_price=0.0,
                    stop_loss=0.0,
                    take_profit=0.0,
                    execute=True,
                )
            )


# ---------------------------------------------------------------------------
# SL / TP requeridos cuando execute=True
# ---------------------------------------------------------------------------


class TestExecuteRequiresSlTp:
    def test_execute_true_with_sl_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="stop_loss > 0"):
            _build(stop_loss=0.0)

    def test_execute_true_with_tp_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="take_profit > 0"):
            _build(take_profit=0.0)


# ---------------------------------------------------------------------------
# Risk/reward mínimo
# ---------------------------------------------------------------------------


class TestMinimumRR:
    def test_rr_above_minimum_accepted(self) -> None:
        d = _build(net_risk_reward=1.5)
        assert d.net_risk_reward == 1.5

    def test_rr_below_minimum_rejected(self) -> None:
        with pytest.raises(ValidationError, match="net_risk_reward >= 1.5"):
            _build(net_risk_reward=1.4)


# ---------------------------------------------------------------------------
# Hard cap de margen
# ---------------------------------------------------------------------------


class TestMarginHardCap:
    def test_margin_at_cap_accepted(self) -> None:
        d = _build(margin_usdt=10.0)
        assert d.margin_usdt == 10.0

    def test_margin_above_cap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(margin_usdt=10.01)

    def test_margin_zero_accepted_no_operar(self) -> None:
        d = ModelDecision.model_validate(
            _long_dict(
                decision="NO_OPERAR",
                entry_type="NO_ENTRY",
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                invalidation_price=0.0,
                margin_usdt=0.0,
                execute=False,
            )
        )
        assert d.margin_usdt == 0.0


# ---------------------------------------------------------------------------
# Caps de leverage por entorno
# ---------------------------------------------------------------------------


class TestLeverageCaps:
    def test_leverage_10_in_paper_accepted(self) -> None:
        assert _build(environment="PAPER", leverage=10).leverage == 10

    def test_leverage_11_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(leverage=11)

    def test_leverage_5_in_testnet_accepted(self) -> None:
        assert _build(environment="TESTNET", leverage=5).leverage == 5

    def test_leverage_6_in_testnet_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cap"):
            _build(environment="TESTNET", leverage=6)

    def test_leverage_5_in_live_accepted(self) -> None:
        assert _build(environment="LIVE", leverage=5).leverage == 5

    def test_leverage_6_in_live_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cap"):
            _build(environment="LIVE", leverage=6)


# ---------------------------------------------------------------------------
# SL / TP coherencia direccional
# ---------------------------------------------------------------------------


class TestSlTpCoherence:
    def test_long_sl_below_entry_accepted(self) -> None:
        d = _build(entry_price=95000.0, stop_loss=90000.0, take_profit=105000.0)
        assert d.stop_loss < d.entry_price < d.take_profit

    def test_long_sl_above_entry_rejected(self) -> None:
        with pytest.raises(ValidationError, match="LONG"):
            _build(entry_price=95000.0, stop_loss=96000.0, take_profit=105000.0)

    def test_long_tp_below_entry_rejected(self) -> None:
        with pytest.raises(ValidationError, match="LONG"):
            _build(entry_price=95000.0, stop_loss=90000.0, take_profit=94000.0)

    def test_short_sl_above_entry_accepted(self) -> None:
        d = _build(
            decision="SHORT",
            entry_price=95000.0,
            stop_loss=100000.0,
            take_profit=85000.0,
        )
        assert d.stop_loss > d.entry_price > d.take_profit

    def test_short_sl_below_entry_rejected(self) -> None:
        with pytest.raises(ValidationError, match="SHORT"):
            _build(
                decision="SHORT",
                entry_price=95000.0,
                stop_loss=90000.0,
                take_profit=85000.0,
            )

    def test_short_tp_above_entry_rejected(self) -> None:
        with pytest.raises(ValidationError, match="SHORT"):
            _build(
                decision="SHORT",
                entry_price=95000.0,
                stop_loss=100000.0,
                take_profit=96000.0,  # TP > entry → inválido para SHORT
            )

    def test_long_entry_price_zero_with_execute_rejected(self) -> None:
        """entry_price=0 con execute=True es rechazado por execute_requires_sl_tp
        (stop_loss=0 no pasa el check stop_loss > 0), documentando el edge case."""
        with pytest.raises(ValidationError, match="stop_loss > 0"):
            _build(entry_price=0.0, stop_loss=0.0, take_profit=0.0)

    def test_sl_tp_not_checked_when_not_execute(self) -> None:
        d = ModelDecision.model_validate(
            _long_dict(
                decision="NO_OPERAR",
                entry_type="NO_ENTRY",
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                invalidation_price=0.0,
                execute=False,
            )
        )
        assert not d.execute


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


class TestConfidence:
    @pytest.mark.parametrize("val", [0.0, 0.5, 1.0])
    def test_valid_confidence_accepted(self, val: float) -> None:
        assert _build(confidence=val).confidence == val

    def test_confidence_above_1_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(confidence=1.01)

    def test_confidence_below_0_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(confidence=-0.01)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class TestEnvironment:
    def test_invalid_environment_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _build(environment="STAGING")

    @pytest.mark.parametrize("env", ["PAPER", "TESTNET", "LIVE"])
    def test_all_environments_accepted(self, env: str) -> None:
        assert _build(environment=env).environment == Environment(env)


# ---------------------------------------------------------------------------
# schema_guard
# ---------------------------------------------------------------------------


class TestSchemaGuard:
    def test_valid_dict_returns_ok(self) -> None:
        result = validate_gpt_response(_long_dict())
        assert result.ok
        assert result.decision is not None
        assert result.errors == []

    def test_result_bool_true_on_ok(self) -> None:
        assert bool(validate_gpt_response(_long_dict())) is True

    def test_invalid_dict_returns_not_ok(self) -> None:
        result = validate_gpt_response({"decision": "LONG"})
        assert not result.ok
        assert result.decision is None
        assert len(result.errors) > 0

    def test_result_bool_false_on_failure(self) -> None:
        assert bool(validate_gpt_response({})) is False

    def test_errors_are_strings(self) -> None:
        result = validate_gpt_response({"decision": "LONG", "symbol": "INVALID"})
        assert all(isinstance(e, str) for e in result.errors)

    def test_margin_over_cap_returns_not_ok(self) -> None:
        result = validate_gpt_response(_long_dict(margin_usdt=15.0))
        assert not result.ok

    def test_accepted_decision_has_correct_type(self) -> None:
        result = validate_gpt_response(_long_dict())
        assert isinstance(result.decision, ModelDecision)
