"""Tests unitarios para DecisionAggregator — sección 4.10.8 del PDF maestro."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.decision_engine.aggregator import (
    _MIN_SCORE_TO_TRADE,
    _REGIME_FACTOR,
    _W_GPT,
    _W_QUANT,
    _W_REGIME,
    _W_VOLATILITY,
    DecisionAggregator,
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
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _snapshot_id() -> str:
    return str(uuid.uuid4())


def _quant_package(
    *,
    momentum_signal: float | None = 0.70,
    mean_reversion_signal: float | None = 0.50,
    breakout_signal: float | None = 0.60,
    signal_strength_score: float | None = 0.65,
    signal_conflict_score: float | None = 0.20,
    signal_confidence: float | None = 0.75,
) -> QuantSignalsPackage:
    sid = _snapshot_id()
    return QuantSignalsPackage(
        snapshot_id=sid,
        timestamp_utc=_now(),
        symbol="BTCUSDT",
        timeframes_used=["15m", "1h"],
        momentum_signal=momentum_signal,
        mean_reversion_signal=mean_reversion_signal,
        breakout_signal=breakout_signal,
        signal_strength_score=signal_strength_score,
        signal_conflict_score=signal_conflict_score,
        signal_confidence=signal_confidence,
    )


def _regime(
    primary: PrimaryRegime = PrimaryRegime.TRENDING,
    confidence: float = 0.80,
) -> MarketRegimeAssessment:
    return MarketRegimeAssessment(
        snapshot_id=_snapshot_id(),
        symbol="BTCUSDT",
        timestamp_utc=_now(),
        primary_regime=primary,
        regime_confidence=confidence,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        funding_state=FundingState.NEUTRAL,
        open_interest_state=OpenInterestState.STABLE,
        trend_alignment=TrendAlignment.BULLISH,
    )


def _volatility(volatility_score: float = 0.30) -> VolatilityAssessmentPackage:
    return VolatilityAssessmentPackage(
        snapshot_id=_snapshot_id(),
        timestamp_utc=_now(),
        symbol="BTCUSDT",
        atr_5m=Decimal("50"),
        atr_15m=Decimal("80"),
        atr_1h=Decimal("120"),
        atr_4h=Decimal("200"),
        atr_percent=0.13,
        realized_vol=0.12,
        volatility_regime=VolatilityRegime.CONTRACTION,
        volatility_score=volatility_score,
        liquidation_risk_score=0.20,
        leverage_cap=5,
        details={},
    )


def _gpt_long_decision(
    *,
    confidence: float = 0.82,
    decision: str = "LONG",
    execute: bool = True,
) -> ModelDecision:
    quant_signals = QuantSignalsSection(
        momentum=MomentumInterpretation.BULLISH,
        mean_reversion=MeanReversionInterpretation.NEUTRAL,
        breakout_detection=BreakoutInterpretation.CONFIRMED,
        funding_analysis=FundingInterpretation.SUPPORTS_TRADE,
        open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
        order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
        liquidity_sweep=LiquiditySweepInterpretation.NONE,
    )
    agg = DecisionAggregatorSection(
        quant_score=0.75,
        gpt_context_score=confidence,
        risk_quality_score=0.70,
        final_trade_quality_score=0.72,
    )
    news = NewsContextSection(used=False, impact=NewsImpact.NEUTRAL, summary="No news.")
    plan = PositionManagementPlan(
        use_trailing_stop=True,
        move_to_break_even=True,
        partial_close_plan="none",
        max_time_in_trade_minutes=120,
    )
    no_entry = decision == "NO_OPERAR"
    return ModelDecision.model_validate(
        {
            "environment": "PAPER",
            "timestamp_utc": _now().isoformat(),
            "decision": decision,
            "symbol": "BTCUSDT",
            "entry_type": "NO_ENTRY" if no_entry else "MARKET",
            "entry_price": 0.0 if no_entry else 95000.0,
            "stop_loss": 0.0 if no_entry else 90000.0,
            "take_profit": 0.0 if no_entry else 105000.0,
            "invalidation_price": 0.0 if no_entry else 89000.0,
            "leverage": 3,
            "margin_usdt": 0.0 if no_entry else 5.0,
            "estimated_notional_usdt": 0.0 if no_entry else 15.0,
            "estimated_entry_fee_usdt": 0.0,
            "estimated_exit_fee_usdt": 0.0,
            "estimated_slippage_usdt": 0.0,
            "estimated_funding_usdt": 0.0,
            "net_risk_reward": 0.0 if no_entry else 2.0,
            "estimated_max_loss_usdt": 0.0 if no_entry else 5.0,
            "liquidation_distance_percent_estimated": 0.0 if no_entry else 18.0,
            "confidence": confidence,
            "market_regime": "TRENDING",
            "setup_name": "momentum_breakout",
            "timeframes_used": ["15m", "1h"],
            "quant_signals": quant_signals.model_dump(),
            "decision_aggregator": agg.model_dump(),
            "news_context": news.model_dump(),
            "position_management_plan": plan.model_dump(),
            "decision_rationale_summary": "Strong momentum.",
            "execute": execute,
        }
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_long_aligned_returns_long(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=_quant_package(),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.final_action == DecisionType.LONG
        assert result.contradictions_detected == []

    def test_result_fields_populated(self) -> None:
        agg = DecisionAggregator()
        gpt = _gpt_long_decision()
        result = agg.aggregate(
            gpt_decision=gpt,
            quant_signals=_quant_package(),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.decision_id == gpt.decision_id
        assert result.symbol == "BTCUSDT"
        assert 0.0 <= result.aggregated_score <= 1.0
        assert result.contributing_sources.quant_score >= 0.0

    def test_aggregation_id_is_uuid(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=_quant_package(),
            regime=_regime(),
            volatility=_volatility(),
        )
        uuid.UUID(result.aggregation_id)  # raises if invalid


# ---------------------------------------------------------------------------
# Contradicción: dirección Quant vs GPT
# ---------------------------------------------------------------------------


class TestDirectionMismatch:
    def test_long_with_bearish_quant_is_no_operar(self) -> None:
        """GPT=LONG pero quant netamente bearish → NO_OPERAR."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(decision="LONG"),
            quant_signals=_quant_package(
                momentum_signal=-0.70,
                mean_reversion_signal=-0.60,
                breakout_signal=-0.50,
                signal_strength_score=0.65,
                signal_conflict_score=0.10,
            ),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("direction_mismatch" in c for c in result.contradictions_detected)

    def test_short_with_bullish_quant_is_no_operar(self) -> None:
        """GPT=SHORT pero quant netamente bullish → NO_OPERAR."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(decision="SHORT", execute=False),
            quant_signals=_quant_package(
                momentum_signal=0.70,
                mean_reversion_signal=0.60,
                breakout_signal=0.50,
            ),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("direction_mismatch" in c for c in result.contradictions_detected)

    def test_aligned_long_no_direction_contradiction(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(decision="LONG"),
            quant_signals=_quant_package(
                momentum_signal=0.70,
                mean_reversion_signal=0.50,
                breakout_signal=0.60,
            ),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert not any("direction_mismatch" in c for c in result.contradictions_detected)


# ---------------------------------------------------------------------------
# Contradicción: conflicto interno quant
# ---------------------------------------------------------------------------


class TestQuantInternalConflict:
    def test_high_conflict_score_triggers_no_operar(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=_quant_package(signal_conflict_score=0.75),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("quant_internal_conflict" in c for c in result.contradictions_detected)

    def test_conflict_score_at_threshold_not_triggered(self) -> None:
        """signal_conflict_score == 0.60 no dispara la contradicción (umbral estricto >)."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=_quant_package(signal_conflict_score=0.60),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert not any("quant_internal_conflict" in c for c in result.contradictions_detected)


# ---------------------------------------------------------------------------
# Contradicción: intensidad quant insuficiente
# ---------------------------------------------------------------------------


class TestLowQuantStrength:
    def test_low_strength_triggers_no_operar(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=_quant_package(signal_strength_score=0.30),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("low_quant_strength" in c for c in result.contradictions_detected)

    def test_strength_at_threshold_not_triggered(self) -> None:
        """signal_strength_score == 0.40 no dispara la contradicción (umbral estricto <)."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=_quant_package(signal_strength_score=0.40),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert not any("low_quant_strength" in c for c in result.contradictions_detected)


# ---------------------------------------------------------------------------
# Contradicción: baja confianza GPT
# ---------------------------------------------------------------------------


class TestLowGptConfidence:
    def test_low_confidence_triggers_no_operar(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(confidence=0.60),
            quant_signals=_quant_package(),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("low_gpt_confidence" in c for c in result.contradictions_detected)

    def test_confidence_at_threshold_not_triggered(self) -> None:
        """confidence == 0.70 no dispara la contradicción (umbral estricto <)."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(confidence=0.70),
            quant_signals=_quant_package(),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert not any("low_gpt_confidence" in c for c in result.contradictions_detected)


# ---------------------------------------------------------------------------
# GPT NO_OPERAR pasa directo
# ---------------------------------------------------------------------------


class TestGptNoOperar:
    def test_gpt_no_operar_always_no_operar(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(decision="NO_OPERAR", execute=False),
            quant_signals=_quant_package(),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.final_action == DecisionType.NO_OPERAR

    def test_gpt_no_operar_no_contradictions_detected(self) -> None:
        """Cuando GPT dice NO_OPERAR no se corre detección de contradicciones."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(decision="NO_OPERAR", execute=False),
            quant_signals=_quant_package(signal_conflict_score=0.90, signal_strength_score=0.10),
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.contradictions_detected == []


# ---------------------------------------------------------------------------
# Score bajo → NO_OPERAR
# ---------------------------------------------------------------------------


class TestLowAggregatedScore:
    def test_unclear_regime_and_high_volatility_drops_below_threshold(self) -> None:
        """Régimen UNCLEAR + alta volatilidad + quant débil cae bajo el umbral mínimo.

        Score esperado (valores fijos):
          quant=0.41, gpt=0.71, regime=0.20 (UNCLEAR), vol=1.0-0.95^1.5*0.60≈0.444
          score ≈ 0.40*0.41 + 0.30*0.71 + 0.20*0.20 + 0.10*0.444 ≈ 0.461
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(confidence=0.71),
            quant_signals=_quant_package(signal_strength_score=0.41, signal_conflict_score=0.10),
            regime=_regime(primary=PrimaryRegime.UNCLEAR),
            volatility=_volatility(volatility_score=0.95),
        )
        assert result.aggregated_score < _MIN_SCORE_TO_TRADE
        assert result.final_action == DecisionType.NO_OPERAR


# ---------------------------------------------------------------------------
# Cálculo de ponderación
# ---------------------------------------------------------------------------


class TestScoreWeighting:
    def test_aggregated_score_uses_documented_weights(self) -> None:
        """Verifica que el score usa los pesos documentados en el módulo."""
        agg = DecisionAggregator()
        qs = _quant_package(signal_strength_score=0.60)
        gpt = _gpt_long_decision(confidence=0.80)
        reg = _regime(primary=PrimaryRegime.TRENDING)  # factor = 0.85
        vol = _volatility(volatility_score=0.30)  # factor = 1.0 - 0.30^1.5 * 0.60 ≈ 0.901

        result = agg.aggregate(gpt_decision=gpt, quant_signals=qs, regime=reg, volatility=vol)

        expected_quant = 0.60
        expected_gpt = 0.80
        expected_regime = _REGIME_FACTOR[PrimaryRegime.TRENDING]
        expected_vol = 1.0 - 0.30**1.5 * 0.60
        expected_score = (
            _W_QUANT * expected_quant
            + _W_GPT * expected_gpt
            + _W_REGIME * expected_regime
            + _W_VOLATILITY * expected_vol
        )
        assert abs(result.aggregated_score - expected_score) < 1e-5

    def test_contributing_sources_reflect_inputs(self) -> None:
        agg = DecisionAggregator()
        qs = _quant_package(signal_strength_score=0.70)
        gpt = _gpt_long_decision(confidence=0.85)
        result = agg.aggregate(
            gpt_decision=gpt,
            quant_signals=qs,
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.contributing_sources.quant_score == pytest.approx(0.70)
        assert result.contributing_sources.gpt_context_score == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# Quant score fallback (sin signal_strength_score)
# ---------------------------------------------------------------------------


class TestQuantScoreFallback:
    def test_fallback_uses_individual_signals(self) -> None:
        """Si signal_strength_score es None, usa promedio de intensidad individual."""
        agg = DecisionAggregator()
        qs = _quant_package(
            signal_strength_score=None,
            momentum_signal=0.60,
            mean_reversion_signal=0.40,
            breakout_signal=0.80,
        )
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=qs,
            regime=_regime(),
            volatility=_volatility(),
        )
        # fallback = mean([0.60, 0.40, 0.80, None, None, None, None] disponibles)
        # = (0.60 + 0.40 + 0.80) / 3 = 0.60
        assert result.contributing_sources.quant_score == pytest.approx(0.60, abs=1e-3)

    def test_fallback_all_none_returns_zero(self) -> None:
        agg = DecisionAggregator()
        qs = _quant_package(
            signal_strength_score=None,
            momentum_signal=None,
            mean_reversion_signal=None,
            breakout_signal=None,
        )
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=qs,
            regime=_regime(),
            volatility=_volatility(),
        )
        assert result.contributing_sources.quant_score == 0.0


# ---------------------------------------------------------------------------
# to_db_kwargs
# ---------------------------------------------------------------------------


class TestToDbKwargs:
    def test_to_db_kwargs_has_required_keys(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=_quant_package(),
            regime=_regime(),
            volatility=_volatility(),
        )
        kwargs = result.to_db_kwargs(bot_run_id=str(uuid.uuid4()))
        required = {
            "id",
            "bot_run_id",
            "decision_id",
            "symbol",
            "timestamp",
            "quant_score",
            "gpt_confidence",
            "regime_factor",
            "volatility_factor",
            "aggregated_score",
            "final_action",
            "reasons",
        }
        assert required.issubset(kwargs.keys())

    def test_to_db_kwargs_final_action_is_string(self) -> None:
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt_long_decision(),
            quant_signals=_quant_package(),
            regime=_regime(),
            volatility=_volatility(),
        )
        kwargs = result.to_db_kwargs(bot_run_id=str(uuid.uuid4()))
        assert isinstance(kwargs["final_action"], str)
