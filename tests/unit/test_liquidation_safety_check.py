"""Tests unitarios — check_liquidation_safety (sección 4.10.9)."""

from __future__ import annotations

from datetime import UTC, datetime

from backend.core.config import LiquidationSafetyConfig
from backend.decision_engine.schemas import (
    BreakoutInterpretation,
    DecisionAggregatorSection,
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
from backend.risk_engine.checks import CheckOutcome, check_liquidation_safety

# ---------------------------------------------------------------------------
# Fixtures helpers
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


def _aggregator() -> DecisionAggregatorSection:
    return DecisionAggregatorSection(
        quant_score=0.8,
        gpt_context_score=0.85,
        risk_quality_score=0.75,
        final_trade_quality_score=0.80,
    )


def _news() -> NewsContextSection:
    return NewsContextSection(used=False, impact=NewsImpact.NEUTRAL, summary="No news.")


def _position_plan() -> PositionManagementPlan:
    return PositionManagementPlan(
        use_trailing_stop=True,
        move_to_break_even=True,
        partial_close_plan="none",
        max_time_in_trade_minutes=120,
    )


def _base_decision(**overrides: object) -> dict[str, object]:
    """Dict base para LONG ejecutable en PAPER con 18% de distancia a liquidación."""
    base: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": _now().isoformat(),
        "decision": "LONG",
        "symbol": "BTCUSDT",
        "entry_type": "MARKET",
        "entry_price": 100_000.0,
        "stop_loss": 92_000.0,  # 8% por debajo del entry
        "take_profit": 116_000.0,
        "invalidation_price": 89_000.0,
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
        "decision_rationale_summary": "Test decision.",
        "execute": True,
    }
    base.update(overrides)
    return base


def _build(**overrides: object) -> ModelDecision:
    return ModelDecision.model_validate(_base_decision(**overrides))


def _default_config(**overrides: object) -> LiquidationSafetyConfig:
    """Config estándar del proyecto: 5% entry-liq, 2% buffer SL-liq."""
    defaults: dict[str, object] = {
        "enabled": True,
        "min_distance_entry_to_liquidation_percent": 5.0,
        "min_buffer_stop_to_liquidation_percent": 2.0,
        "action_if_invalid": "ADJUST_DOWN_OR_BLOCK",
        "block_if_liquidation_unknown": True,
        "block_if_stop_after_liquidation": True,
    }
    defaults.update(overrides)
    return LiquidationSafetyConfig(**defaults)


# ---------------------------------------------------------------------------
# Tests: check deshabilitado
# ---------------------------------------------------------------------------


class TestCheckDisabled:
    def test_disabled_always_passes(self) -> None:
        decision = _build()
        config = _default_config(enabled=False)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS

    def test_disabled_passes_even_with_sl_past_liquidation(self) -> None:
        # entry=100k, liq=82k (18% abajo), SL=80k (past liq)
        decision = _build(stop_loss=80_000.0)
        config = _default_config(enabled=False)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# Tests: NO_OPERAR
# ---------------------------------------------------------------------------


class TestNoOperar:
    def test_no_operar_always_passes(self) -> None:
        decision = _build(
            decision="NO_OPERAR",
            entry_type="NO_ENTRY",
            stop_loss=0.0,
            take_profit=0.0,
            liquidation_distance_percent_estimated=0.0,
            net_risk_reward=0.0,
            execute=False,
        )
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# Tests: distancia de liquidación desconocida (0%)
# ---------------------------------------------------------------------------


class TestLiquidationUnknown:
    def test_unknown_liq_blocks_when_configured(self) -> None:
        decision = _build(liquidation_distance_percent_estimated=0.0)
        config = _default_config(block_if_liquidation_unknown=True)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "liquidation_unknown"

    def test_unknown_liq_passes_when_not_configured(self) -> None:
        decision = _build(liquidation_distance_percent_estimated=0.0)
        config = _default_config(block_if_liquidation_unknown=False)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# Tests: LONG — SL más allá del precio de liquidación
# ---------------------------------------------------------------------------


class TestLongSlPastLiquidation:
    def test_sl_below_liq_price_blocks(self) -> None:
        # entry=100k, liq_dist=18% → liq=82k; SL=80k (abajo de liq)
        decision = _build(stop_loss=80_000.0)
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "sl_after_liquidation"

    def test_sl_equal_to_liq_price_blocks(self) -> None:
        # SL justo en el precio de liquidación = igualmente inválido
        entry = 100_000.0
        liq_dist = 18.0
        liq_price = entry * (1 - liq_dist / 100)  # 82000.0
        decision = _build(stop_loss=liq_price)
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "sl_after_liquidation"

    def test_sl_past_liq_passes_when_block_disabled(self) -> None:
        decision = _build(stop_loss=80_000.0)
        config = _default_config(block_if_stop_after_liquidation=False)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# Tests: SHORT — SL más allá del precio de liquidación
# ---------------------------------------------------------------------------


class TestShortSlPastLiquidation:
    def test_sl_above_liq_price_blocks(self) -> None:
        # SHORT: entry=100k, liq_dist=18% → liq=118k; SL=120k (encima de liq)
        decision = _build(
            decision="SHORT",
            stop_loss=120_000.0,
            take_profit=84_000.0,
        )
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "sl_after_liquidation"

    def test_sl_equal_to_liq_price_blocks(self) -> None:
        entry = 100_000.0
        liq_dist = 18.0
        liq_price = entry * (1 + liq_dist / 100)  # 118000.0
        decision = _build(
            decision="SHORT",
            stop_loss=liq_price,
            take_profit=84_000.0,
        )
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "sl_after_liquidation"

    def test_sl_past_liq_passes_when_block_disabled(self) -> None:
        # SHORT: SL encima de liq, pero block_if_stop_after_liquidation=False
        decision = _build(
            decision="SHORT",
            stop_loss=120_000.0,
            take_profit=84_000.0,
        )
        config = _default_config(block_if_stop_after_liquidation=False)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# Tests: distancia entry→liquidación demasiado pequeña
# ---------------------------------------------------------------------------


class TestEntryLiqDistance:
    def test_distance_below_minimum_triggers_adjust_down(self) -> None:
        # LONG: entry=100k, liq_dist=3% → liq_price=97k
        # SL=99k: encima de liq (correcto) y buffer=(99k-97k)/97k*100≈2.06% > 2% (ok)
        # Falla únicamente por entry_liq_distance: 3% < 5%
        decision = _build(
            liquidation_distance_percent_estimated=3.0,
            stop_loss=99_000.0,
        )
        config = _default_config(min_distance_entry_to_liquidation_percent=5.0)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert result.rule == "entry_liq_distance"

    def test_distance_equal_to_minimum_passes(self) -> None:
        # liq_dist=5% == min 5%: no falla
        # entry=100k, liq=95k; SL debe estar bastante arriba de liq
        decision = _build(
            liquidation_distance_percent_estimated=5.0,
            stop_loss=98_000.0,  # 3.16% por encima de liq (95k) → buffer >2%
        )
        config = _default_config(min_distance_entry_to_liquidation_percent=5.0)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS

    def test_distance_above_minimum_passes(self) -> None:
        decision = _build(liquidation_distance_percent_estimated=18.0)
        config = _default_config(min_distance_entry_to_liquidation_percent=5.0)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# Tests: buffer SL→liquidación demasiado pequeño
# ---------------------------------------------------------------------------


class TestSlLiqBuffer:
    def test_insufficient_buffer_long_triggers_adjust_down(self) -> None:
        # entry=100k, liq_dist=18% → liq=82k
        # SL=83_640.0 → buffer = (83640-82000)/82000 * 100 ≈ 2.0% (justo en el límite)
        # SL=83_000 → buffer = (83000-82000)/82000 * 100 ≈ 1.22% < 2%
        decision = _build(stop_loss=83_000.0)
        config = _default_config(min_buffer_stop_to_liquidation_percent=2.0)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert result.rule == "sl_liq_buffer"

    def test_sufficient_buffer_long_passes(self) -> None:
        # SL=84_000 → buffer = (84000-82000)/82000 * 100 ≈ 2.44% > 2%
        decision = _build(stop_loss=84_000.0)
        config = _default_config(min_buffer_stop_to_liquidation_percent=2.0)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS

    def test_insufficient_buffer_short_triggers_adjust_down(self) -> None:
        # SHORT: entry=100k, liq_dist=18% → liq=118k
        # SL=116_500 → buffer = (118000-116500)/118000 * 100 ≈ 1.27% < 2%
        decision = _build(
            decision="SHORT",
            stop_loss=116_500.0,
            take_profit=84_000.0,
        )
        config = _default_config(min_buffer_stop_to_liquidation_percent=2.0)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.ADJUST_DOWN
        assert result.rule == "sl_liq_buffer"

    def test_sufficient_buffer_short_passes(self) -> None:
        # SL=115_000 → buffer = (118000-115000)/118000 * 100 ≈ 2.54% > 2%
        decision = _build(
            decision="SHORT",
            stop_loss=115_000.0,
            take_profit=84_000.0,
        )
        config = _default_config(min_buffer_stop_to_liquidation_percent=2.0)
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS


# ---------------------------------------------------------------------------
# Tests: happy path completo
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_long_with_all_conditions_met_passes(self) -> None:
        # entry=100k, liq_dist=18% → liq=82k
        # SL=92k → buffer = (92000-82000)/82000 * 100 ≈ 12.2% >> 2%
        decision = _build()
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS
        assert result.rule == "liquidation_safety"
        assert "LONG" in result.reason

    def test_short_with_all_conditions_met_passes(self) -> None:
        # SHORT: entry=100k, liq_dist=18% → liq=118k
        # SL=108k → buffer = (118000-108000)/118000 * 100 ≈ 8.47% >> 2%
        decision = _build(
            decision="SHORT",
            stop_loss=108_000.0,
            take_profit=84_000.0,
        )
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS
        assert "SHORT" in result.reason

    def test_reason_contains_buffer_and_distance_info(self) -> None:
        decision = _build()
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.PASS
        assert "%" in result.reason


# ---------------------------------------------------------------------------
# Tests: reason messages
# ---------------------------------------------------------------------------


class TestReasonMessages:
    def test_block_reason_mentions_sl_and_liq_price(self) -> None:
        decision = _build(stop_loss=80_000.0)
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert "80000" in result.reason or "80_000" in result.reason or "stop_loss" in result.reason

    def test_adjust_down_reason_mentions_percentages(self) -> None:
        decision = _build(stop_loss=83_000.0)
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert "%" in result.reason


# ---------------------------------------------------------------------------
# Tests: guard entry_price inválido
# ---------------------------------------------------------------------------


class TestEntryPriceGuard:
    def test_zero_entry_price_blocks(self) -> None:
        decision = _build()
        # Bypass frozen model to inject invalid entry_price
        object.__setattr__(decision, "entry_price", 0.0)
        config = _default_config()
        result = check_liquidation_safety(decision, config)
        assert result.outcome == CheckOutcome.BLOCK
        assert "entry_price" in result.reason
