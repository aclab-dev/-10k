"""Tests unitarios — Risk Engine: validaciones F9 (sección 4.10.9)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.core.config import AppConfig, Environment, get_config
from backend.decision_engine.aggregator_schemas import (
    ContributingSources,
    DecisionAggregationResult,
)
from backend.decision_engine.schemas import (
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
from backend.market_regime.schemas import PrimaryRegime
from backend.risk_engine.checks import (
    CheckOutcome,
    check_daily_drawdown,
    check_leverage_cap,
    check_margin_cap,
    check_sl_required,
    check_total_drawdown,
    check_tp_or_exit_plan,
    leverage_cap_for_env,
)
from backend.risk_engine.engine import validate
from backend.risk_engine.schemas import RiskDecision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _quant_signals() -> QuantSignalsSection:
    return QuantSignalsSection(
        momentum=MomentumInterpretation.BULLISH,
        mean_reversion=MeanReversionInterpretation.NEUTRAL,
        breakout_detection=BreakoutInterpretation.CONFIRMED,
        funding_analysis=FundingInterpretation.SUPPORTS_TRADE,
        open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
        order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
        liquidity_sweep=LiquiditySweepInterpretation.NONE,
    )


def _aggregator_section() -> DecisionAggregatorSection:
    return DecisionAggregatorSection(
        quant_score=0.8,
        gpt_context_score=0.85,
        risk_quality_score=0.75,
        final_trade_quality_score=0.80,
    )


def _news() -> NewsContextSection:
    return NewsContextSection(used=False, impact=NewsImpact.NEUTRAL, summary="No news.")


def _position_plan(
    *,
    use_trailing_stop: bool = True,
    move_to_break_even: bool = True,
    partial_close_plan: str = "none",
    max_time_in_trade_minutes: int = 120,
) -> PositionManagementPlan:
    return PositionManagementPlan(
        use_trailing_stop=use_trailing_stop,
        move_to_break_even=move_to_break_even,
        partial_close_plan=partial_close_plan,
        max_time_in_trade_minutes=max_time_in_trade_minutes,
    )


def _long_decision(**overrides: object) -> ModelDecision:
    """LONG ejecutable válido en PAPER con parámetros conservadores."""
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
        "market_regime": PrimaryRegime.TRENDING.value,
        "setup_name": "momentum_breakout",
        "timeframes_used": ["15m", "1h", "4h"],
        "quant_signals": _quant_signals().model_dump(),
        "decision_aggregator": _aggregator_section().model_dump(),
        "news_context": _news().model_dump(),
        "position_management_plan": _position_plan().model_dump(),
        "decision_rationale_summary": "Strong momentum with confirmed breakout.",
        "execute": True,
    }
    base.update(overrides)
    return ModelDecision.model_validate(base)


def _aggregation(decision: ModelDecision) -> DecisionAggregationResult:
    return DecisionAggregationResult(
        decision_id=decision.decision_id,
        symbol=decision.symbol,
        timestamp_utc=_now(),
        contributing_sources=ContributingSources(
            quant_score=0.80,
            gpt_context_score=0.85,
            regime_factor=0.75,
            volatility_factor=0.70,
        ),
        aggregated_score=0.78,
        final_action=DecisionType.LONG,
    )


def _config() -> AppConfig:
    return get_config()


def _config_with_lower_margin_cap(cap: float) -> AppConfig:
    """Config con tope de margen por debajo del máximo del schema (10 USDT)."""
    cfg = _config()
    custom_risk = cfg.risk.model_copy(update={"max_margin_per_trade_usdt": cap})
    return cfg.model_copy(update={"risk": custom_risk})


def _config_with_lower_leverage_cap_paper(cap: int) -> AppConfig:
    """Config con tope de leverage PAPER por debajo del máximo del schema (10x)."""
    cfg = _config()
    custom_leverage = cfg.leverage.model_copy(update={"max_leverage_paper": cap})
    return cfg.model_copy(update={"leverage": custom_leverage})


# ---------------------------------------------------------------------------
# Tests: funciones de check individuales
# ---------------------------------------------------------------------------


class TestCheckSlRequired:
    def test_passes_when_sl_present(self) -> None:
        decision = _long_decision(stop_loss=90000.0, execute=True)
        result = check_sl_required(decision)
        assert result.outcome == CheckOutcome.PASS

    def test_passes_when_not_execute(self) -> None:
        decision = _long_decision(decision="NO_OPERAR", execute=False, stop_loss=0.0)
        result = check_sl_required(decision)
        assert result.outcome == CheckOutcome.PASS

    def test_blocks_when_sl_zero_and_execute(self) -> None:
        # Construir manualmente para bypassear el validator del schema que ya exige SL>0

        decision = _long_decision()
        patched = decision.model_copy(update={"stop_loss": 0.0})
        # Nota: model_copy es frozen, así que usamos object.__setattr__ en tests
        object.__setattr__(patched, "stop_loss", 0.0)
        result = check_sl_required(patched)
        assert result.outcome == CheckOutcome.BLOCK
        assert "sl_required" == result.rule


class TestCheckTpOrExitPlan:
    def test_passes_with_tp(self) -> None:
        decision = _long_decision(take_profit=105000.0)
        result = check_tp_or_exit_plan(decision)
        assert result.outcome == CheckOutcome.PASS

    def test_passes_when_not_execute(self) -> None:
        decision = _long_decision(decision="NO_OPERAR", execute=False, take_profit=0.0)
        result = check_tp_or_exit_plan(decision)
        assert result.outcome == CheckOutcome.PASS
        assert "No aplica" in result.reason

    def test_passes_with_trailing_stop_only(self) -> None:
        plan = _position_plan(
            use_trailing_stop=True,
            move_to_break_even=False,
            partial_close_plan="",
            max_time_in_trade_minutes=0,
        )
        decision = _long_decision(position_management_plan=plan.model_dump())
        patched = decision.model_copy()
        object.__setattr__(patched, "take_profit", 0.0)
        result = check_tp_or_exit_plan(patched)
        assert result.outcome == CheckOutcome.PASS

    def test_passes_with_time_limit_only(self) -> None:
        plan = _position_plan(
            use_trailing_stop=False,
            move_to_break_even=False,
            partial_close_plan="",
            max_time_in_trade_minutes=60,
        )
        decision = _long_decision(position_management_plan=plan.model_dump())
        patched = decision.model_copy()
        object.__setattr__(patched, "take_profit", 0.0)
        result = check_tp_or_exit_plan(patched)
        assert result.outcome == CheckOutcome.PASS

    def test_blocks_when_no_tp_and_no_exit_plan(self) -> None:
        plan = _position_plan(
            use_trailing_stop=False,
            move_to_break_even=False,
            partial_close_plan="",
            max_time_in_trade_minutes=0,
        )
        decision = _long_decision(position_management_plan=plan.model_dump())
        patched = decision.model_copy()
        object.__setattr__(patched, "take_profit", 0.0)
        result = check_tp_or_exit_plan(patched)
        assert result.outcome == CheckOutcome.BLOCK


class TestCheckMarginCap:
    def test_passes_when_within_cap(self) -> None:
        result = check_margin_cap(Decimal("5.0"), Decimal("10.0"))
        assert result.outcome == CheckOutcome.PASS

    def test_passes_at_exact_cap(self) -> None:
        result = check_margin_cap(Decimal("10.0"), Decimal("10.0"))
        assert result.outcome == CheckOutcome.PASS

    def test_adjust_down_when_above_cap(self) -> None:
        result = check_margin_cap(Decimal("8.0"), Decimal("5.0"))
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert "margin_cap" == result.rule

    def test_reason_contains_values(self) -> None:
        result = check_margin_cap(Decimal("8.0"), Decimal("5.0"))
        assert "8.0" in result.reason or "8" in result.reason
        assert "5.0" in result.reason or "5" in result.reason


class TestCheckLeverageCap:
    def test_passes_within_paper_cap(self) -> None:
        cfg = _config()
        result = check_leverage_cap(5, cfg, Environment.PAPER)
        assert result.outcome == CheckOutcome.PASS

    def test_passes_at_exact_paper_cap(self) -> None:
        cfg = _config()
        result = check_leverage_cap(10, cfg, Environment.PAPER)
        assert result.outcome == CheckOutcome.PASS

    def test_adjust_down_exceeds_testnet_cap(self) -> None:
        cfg = _config()  # TESTNET cap = 5
        result = check_leverage_cap(6, cfg, Environment.TESTNET)
        assert result.outcome == CheckOutcome.ADJUST_DOWN

    def test_passes_within_testnet_cap(self) -> None:
        cfg = _config()
        result = check_leverage_cap(5, cfg, Environment.TESTNET)
        assert result.outcome == CheckOutcome.PASS

    def test_passes_within_live_absolute_cap(self) -> None:
        cfg = _config()  # LIVE absolute cap = 5
        result = check_leverage_cap(5, cfg, Environment.LIVE)
        assert result.outcome == CheckOutcome.PASS

    def test_adjust_down_exceeds_live_cap(self) -> None:
        cfg = _config()
        result = check_leverage_cap(6, cfg, Environment.LIVE)
        assert result.outcome == CheckOutcome.ADJUST_DOWN


class TestCheckDailyDrawdown:
    def test_passes_when_below_limit(self) -> None:
        result = check_daily_drawdown(
            daily_loss_usdt=Decimal("5.0"),
            initial_balance_usdt=Decimal("100.0"),
            max_daily_loss_percent=10.0,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_blocks_when_at_limit(self) -> None:
        result = check_daily_drawdown(
            daily_loss_usdt=Decimal("10.0"),
            initial_balance_usdt=Decimal("100.0"),
            max_daily_loss_percent=10.0,
        )
        assert result.outcome == CheckOutcome.BLOCK

    def test_blocks_when_above_limit(self) -> None:
        result = check_daily_drawdown(
            daily_loss_usdt=Decimal("11.0"),
            initial_balance_usdt=Decimal("100.0"),
            max_daily_loss_percent=10.0,
        )
        assert result.outcome == CheckOutcome.BLOCK

    def test_passes_when_zero_loss(self) -> None:
        result = check_daily_drawdown(
            daily_loss_usdt=Decimal("0.0"),
            initial_balance_usdt=Decimal("100.0"),
            max_daily_loss_percent=10.0,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_blocks_when_invalid_balance(self) -> None:
        result = check_daily_drawdown(
            daily_loss_usdt=Decimal("5.0"),
            initial_balance_usdt=Decimal("0"),
            max_daily_loss_percent=10.0,
        )
        assert result.outcome == CheckOutcome.BLOCK

    def test_reason_contains_percentages(self) -> None:
        result = check_daily_drawdown(
            daily_loss_usdt=Decimal("5.0"),
            initial_balance_usdt=Decimal("100.0"),
            max_daily_loss_percent=10.0,
        )
        assert "5.00%" in result.reason or "5.0" in result.reason


class TestCheckTotalDrawdown:
    def test_passes_when_below_limit(self) -> None:
        result = check_total_drawdown(
            total_loss_usdt=Decimal("20.0"),
            initial_balance_usdt=Decimal("100.0"),
            max_total_loss_percent=50.0,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_blocks_when_at_limit(self) -> None:
        result = check_total_drawdown(
            total_loss_usdt=Decimal("50.0"),
            initial_balance_usdt=Decimal("100.0"),
            max_total_loss_percent=50.0,
        )
        assert result.outcome == CheckOutcome.BLOCK

    def test_blocks_when_above_limit(self) -> None:
        result = check_total_drawdown(
            total_loss_usdt=Decimal("60.0"),
            initial_balance_usdt=Decimal("100.0"),
            max_total_loss_percent=50.0,
        )
        assert result.outcome == CheckOutcome.BLOCK

    def test_blocks_when_invalid_balance(self) -> None:
        result = check_total_drawdown(
            total_loss_usdt=Decimal("5.0"),
            initial_balance_usdt=Decimal("0"),
            max_total_loss_percent=50.0,
        )
        assert result.outcome == CheckOutcome.BLOCK


class TestLeverageCapForEnv:
    def test_paper_returns_max_paper(self) -> None:
        cfg = _config()
        assert leverage_cap_for_env(cfg, Environment.PAPER) == cfg.leverage.max_leverage_paper

    def test_testnet_returns_max_testnet(self) -> None:
        cfg = _config()
        assert leverage_cap_for_env(cfg, Environment.TESTNET) == cfg.leverage.max_leverage_testnet

    def test_live_returns_absolute_cap(self) -> None:
        cfg = _config()
        assert (
            leverage_cap_for_env(cfg, Environment.LIVE) == cfg.leverage.max_leverage_live_absolute
        )


# ---------------------------------------------------------------------------
# Tests: engine — APPROVE
# ---------------------------------------------------------------------------


class TestRiskEngineApprove:
    def test_approve_when_all_checks_pass(self) -> None:
        cfg = _config()
        decision = _long_decision(leverage=5, margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE

    def test_approve_preserves_original_parameters(self) -> None:
        cfg = _config()
        decision = _long_decision(leverage=5, margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.original_margin_usdt == Decimal("5.0")
        assert result.original_leverage == 5
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.margin_usdt == Decimal("5.0")
        assert result.adjusted_parameters.leverage == 5

    def test_approve_links_correct_aggregation_id(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.aggregation_id == aggregation.aggregation_id

    def test_approve_has_correct_symbol(self) -> None:
        cfg = _config()
        decision = _long_decision(
            symbol="ETHUSDT",
            entry_price=3000.0,
            stop_loss=2800.0,
            take_profit=3400.0,
            invalidation_price=2700.0,
        )
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.symbol == "ETHUSDT"

    def test_approve_has_utc_timestamp(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        from datetime import timedelta

        assert result.timestamp_utc.utcoffset() == timedelta(0)

    def test_approve_records_loss_snapshot(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("3.0"), Decimal("8.0"), cfg)
        assert result.daily_loss_at_check_usdt == Decimal("3.0")
        assert result.total_loss_at_check_usdt == Decimal("8.0")

    def test_approve_reasons_contain_all_rules(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert "sl_required" in result.reasons
        assert "tp_or_exit_plan" in result.reasons
        assert "daily_drawdown" in result.reasons
        assert "total_drawdown" in result.reasons
        assert "margin_cap" in result.reasons
        assert "leverage_cap" in result.reasons

    def test_approve_with_zero_losses(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE

    def test_approve_near_daily_drawdown_limit(self) -> None:
        # 9.9 USDT de 100 USDT balance = 9.9% < 10% límite
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("9.9"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE

    def test_approve_near_total_drawdown_limit(self) -> None:
        # 49.9 USDT de 100 USDT balance = 49.9% < 50% límite
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("49.9"), cfg)
        assert result.decision == RiskDecision.APPROVE


# ---------------------------------------------------------------------------
# Tests: engine — BLOCK
# ---------------------------------------------------------------------------


class TestRiskEngineBlock:
    def test_block_when_daily_drawdown_reached(self) -> None:
        # 10 USDT de 100 USDT = 10% exacto → BLOCK
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert "daily_drawdown" in result.reasons

    def test_block_when_daily_drawdown_exceeded(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("15.0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK

    def test_block_when_total_drawdown_reached(self) -> None:
        # 50 USDT de 100 USDT = 50% exacto → BLOCK
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("50.0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert "total_drawdown" in result.reasons

    def test_block_when_total_drawdown_exceeded(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("75.0"), cfg)
        assert result.decision == RiskDecision.BLOCK

    def test_block_has_no_adjusted_parameters(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert result.adjusted_parameters is None

    def test_block_records_loss_snapshot(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("30.0"), cfg)
        assert result.daily_loss_at_check_usdt == Decimal("10.0")
        assert result.total_loss_at_check_usdt == Decimal("30.0")

    def test_block_when_both_drawdown_limits_exceeded(self) -> None:
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("12.0"), Decimal("55.0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert "daily_drawdown" in result.reasons
        assert "total_drawdown" in result.reasons

    def test_block_reasons_include_passing_checks_too(self) -> None:
        """Auditoría: los motivos de BLOCK incluyen todos los checks, no solo los fallidos."""
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert "sl_required" in result.reasons
        assert "tp_or_exit_plan" in result.reasons


# ---------------------------------------------------------------------------
# Tests: engine — ADJUST_DOWN
# ---------------------------------------------------------------------------


class TestRiskEngineAdjustDown:
    def test_adjust_down_when_margin_exceeds_config_cap(self) -> None:
        # Config cap = 3 USDT, decisión margin = 5 USDT → ADJUST_DOWN
        cfg = _config_with_lower_margin_cap(3.0)
        decision = _long_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.ADJUST_DOWN

    def test_adjust_down_reduces_margin_to_cap(self) -> None:
        cfg = _config_with_lower_margin_cap(3.0)
        decision = _long_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.margin_usdt == Decimal("3.0")

    def test_adjust_down_preserves_original_margin(self) -> None:
        cfg = _config_with_lower_margin_cap(3.0)
        decision = _long_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.original_margin_usdt == Decimal("5.0")

    def test_adjust_down_when_leverage_exceeds_lower_config_cap(self) -> None:
        # PAPER cap config = 3x, decisión leverage = 5 → ADJUST_DOWN
        cfg = _config_with_lower_leverage_cap_paper(3)
        decision = _long_decision(leverage=5)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.ADJUST_DOWN

    def test_adjust_down_reduces_leverage_to_cap(self) -> None:
        cfg = _config_with_lower_leverage_cap_paper(3)
        decision = _long_decision(leverage=5)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.leverage == 3

    def test_adjust_down_when_both_exceed_caps(self) -> None:
        cfg = _config()
        # Reducir ambos caps usando model_copy combinado
        custom_risk = cfg.risk.model_copy(update={"max_margin_per_trade_usdt": 3.0})
        custom_leverage = cfg.leverage.model_copy(update={"max_leverage_paper": 3})
        cfg_custom = cfg.model_copy(update={"risk": custom_risk, "leverage": custom_leverage})
        decision = _long_decision(margin_usdt=5.0, leverage=5)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg_custom)
        assert result.decision == RiskDecision.ADJUST_DOWN
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.margin_usdt == Decimal("3.0")
        assert result.adjusted_parameters.leverage == 3

    def test_adjust_down_reasons_include_all_checks(self) -> None:
        cfg = _config_with_lower_margin_cap(3.0)
        decision = _long_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        expected_rules = (
            "sl_required",
            "tp_or_exit_plan",
            "daily_drawdown",
            "total_drawdown",
            "margin_cap",
            "leverage_cap",
        )
        for rule in expected_rules:
            assert rule in result.reasons

    def test_adjust_down_does_not_raise_on_drawdown_below_limit(self) -> None:
        cfg = _config_with_lower_margin_cap(3.0)
        decision = _long_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        # 5 USDT de 100 = 5% < 10% límite → no bloquea por drawdown
        result = validate(aggregation, decision, Decimal("5.0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.ADJUST_DOWN

    def test_adjust_down_blocked_when_drawdown_also_exceeded(self) -> None:
        """BLOCK tiene precedencia sobre ADJUST_DOWN."""
        cfg = _config_with_lower_margin_cap(3.0)
        decision = _long_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        # Drawdown excedido Y margen fuera de cap → BLOCK (drawdown gana)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK


# ---------------------------------------------------------------------------
# Tests: engine — precondiciones
# ---------------------------------------------------------------------------


class TestRiskEnginePreconditions:
    def test_raises_when_execute_false(self) -> None:
        cfg = _config()
        decision = _long_decision(
            decision="NO_OPERAR",
            execute=False,
            take_profit=0.0,
            stop_loss=0.0,
            margin_usdt=0.0,
            leverage=1,
            net_risk_reward=0.0,
        )
        aggregation = _aggregation(decision)
        with pytest.raises(ValueError, match="execute=True"):
            validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

    def test_raises_when_margin_zero(self) -> None:
        cfg = _config()
        decision = _long_decision(margin_usdt=5.0)
        patched = decision.model_copy()
        object.__setattr__(patched, "margin_usdt", 0.0)
        aggregation = _aggregation(decision)
        with pytest.raises(ValueError, match="margin_usdt"):
            validate(aggregation, patched, Decimal("0"), Decimal("0"), cfg)

    def test_result_is_immutable(self) -> None:
        from pydantic import ValidationError

        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        with pytest.raises((ValidationError, TypeError)):
            result.decision = RiskDecision.BLOCK  # type: ignore[misc]
