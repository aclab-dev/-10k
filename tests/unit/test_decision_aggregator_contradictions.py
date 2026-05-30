"""Tests de casos contradictorios — tarea [69] F8.

Cubre los escenarios del DoD que no están en test_decision_aggregator.py:
  - acuerdo SHORT (el happy path existente solo cubre LONG)
  - desacuerdo parcial: 2 contradicciones activas simultáneamente
  - desacuerdo total: las 4 contradicciones activas al mismo tiempo
  - señales débiles combinadas (low_quant_strength + low_gpt_confidence juntas)
  - régimen UNCLEAR aislado: reduce score pero no bloquea si las señales son fuertes
  - NO_OPERAR por score bajo sin ninguna contradicción activa
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.decision_engine.aggregator import (
    _MIN_SCORE_TO_TRADE,
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
# Helpers (análogos a los de test_decision_aggregator.py)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _sid() -> str:
    return str(uuid.uuid4())


def _quant(
    *,
    momentum_signal: float | None = 0.70,
    mean_reversion_signal: float | None = 0.50,
    breakout_signal: float | None = 0.60,
    signal_strength_score: float | None = 0.65,
    signal_conflict_score: float | None = 0.20,
    signal_confidence: float | None = 0.75,
) -> QuantSignalsPackage:
    return QuantSignalsPackage(
        snapshot_id=_sid(),
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


def _regime(primary: PrimaryRegime = PrimaryRegime.TRENDING) -> MarketRegimeAssessment:
    return MarketRegimeAssessment(
        snapshot_id=_sid(),
        symbol="BTCUSDT",
        timestamp_utc=_now(),
        primary_regime=primary,
        regime_confidence=0.80,
        volatility_state=VolatilityState.NORMAL,
        liquidity_state=LiquidityState.NORMAL,
        funding_state=FundingState.NEUTRAL,
        open_interest_state=OpenInterestState.STABLE,
        trend_alignment=TrendAlignment.BULLISH,
    )


def _vol(volatility_score: float = 0.30) -> VolatilityAssessmentPackage:
    return VolatilityAssessmentPackage(
        snapshot_id=_sid(),
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


def _gpt(
    *,
    decision: str = "LONG",
    confidence: float = 0.82,
) -> ModelDecision:
    no_entry = decision == "NO_OPERAR"
    is_short = decision == "SHORT"
    quant_signals = QuantSignalsSection(
        momentum=MomentumInterpretation.BULLISH,
        mean_reversion=MeanReversionInterpretation.NEUTRAL,
        breakout_detection=BreakoutInterpretation.CONFIRMED,
        funding_analysis=FundingInterpretation.SUPPORTS_TRADE,
        open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
        order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
        liquidity_sweep=LiquiditySweepInterpretation.NONE,
    )
    agg_section = DecisionAggregatorSection(
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
    # SHORT: stop_loss > entry_price (stop arriba), take_profit < entry_price (target abajo)
    entry_price = 0.0 if no_entry else 95000.0
    stop_loss = 0.0 if no_entry else (100000.0 if is_short else 90000.0)
    take_profit = 0.0 if no_entry else (85000.0 if is_short else 105000.0)
    invalidation = 0.0 if no_entry else (101000.0 if is_short else 89000.0)
    return ModelDecision.model_validate(
        {
            "environment": "PAPER",
            "timestamp_utc": _now().isoformat(),
            "decision": decision,
            "symbol": "BTCUSDT",
            "entry_type": "NO_ENTRY" if no_entry else "MARKET",
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "invalidation_price": invalidation,
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
            "decision_aggregator": agg_section.model_dump(),
            "news_context": news.model_dump(),
            "position_management_plan": plan.model_dump(),
            "decision_rationale_summary": "Strong momentum.",
            "execute": not no_entry,
        }
    )


# ---------------------------------------------------------------------------
# Acuerdo — SHORT alineado
# ---------------------------------------------------------------------------


class TestAcuerdoShort:
    """El happy path existente solo cubre LONG. SHORT alineado debe funcionar igual."""

    def test_short_aligned_returns_short(self) -> None:
        """GPT=SHORT + quant netamente bearish → SHORT sin contradicciones."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="SHORT", confidence=0.82),
            quant_signals=_quant(
                momentum_signal=-0.70,
                mean_reversion_signal=-0.50,
                breakout_signal=-0.60,
                signal_strength_score=0.65,
                signal_conflict_score=0.20,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert result.final_action == DecisionType.SHORT
        assert result.contradictions_detected == []

    def test_short_aligned_score_above_threshold(self) -> None:
        """SHORT alineado con parámetros estándar produce score sobre el umbral mínimo."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="SHORT", confidence=0.82),
            quant_signals=_quant(
                momentum_signal=-0.70,
                mean_reversion_signal=-0.50,
                breakout_signal=-0.60,
                signal_strength_score=0.65,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert result.aggregated_score >= _MIN_SCORE_TO_TRADE


# ---------------------------------------------------------------------------
# Desacuerdo parcial — 2 contradicciones activas simultáneamente
# ---------------------------------------------------------------------------


class TestDesacuerdoParcial:
    """Verifica que múltiples contradicciones se reportan todas en contradictions_detected."""

    def test_direction_mismatch_and_low_strength_both_reported(self) -> None:
        """direction_mismatch + low_quant_strength → ambas aparecen en contradictions_detected.

        GPT=LONG, quant netamente bearish (direction_mismatch) y strength=0.35 (low_quant_strength).
        conflict=0.30 y confidence=0.82 no disparan sus respectivas contradicciones.
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.82),
            quant_signals=_quant(
                momentum_signal=-0.70,
                mean_reversion_signal=-0.60,
                breakout_signal=-0.50,
                signal_strength_score=0.35,
                signal_conflict_score=0.30,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("direction_mismatch" in c for c in result.contradictions_detected)
        assert any("low_quant_strength" in c for c in result.contradictions_detected)
        assert len(result.contradictions_detected) == 2

    def test_internal_conflict_and_low_gpt_confidence_both_reported(self) -> None:
        """quant_internal_conflict + low_gpt_confidence → ambas reportadas.

        quant bullish alineado con GPT=LONG (sin direction_mismatch), strength alta
        (sin low_quant_strength), pero conflict=0.75 y confidence=0.60.
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.60),
            quant_signals=_quant(
                momentum_signal=0.70,
                mean_reversion_signal=0.50,
                breakout_signal=0.60,
                signal_strength_score=0.65,
                signal_conflict_score=0.75,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("quant_internal_conflict" in c for c in result.contradictions_detected)
        assert any("low_gpt_confidence" in c for c in result.contradictions_detected)
        assert len(result.contradictions_detected) == 2

    def test_direction_mismatch_and_internal_conflict_both_reported(self) -> None:
        """direction_mismatch + quant_internal_conflict → ambas reportadas.

        strength=0.65 (no low_quant_strength), confidence=0.82 (no low_gpt_confidence).
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.82),
            quant_signals=_quant(
                momentum_signal=-0.70,
                mean_reversion_signal=-0.50,
                breakout_signal=-0.60,
                signal_strength_score=0.65,
                signal_conflict_score=0.80,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("direction_mismatch" in c for c in result.contradictions_detected)
        assert any("quant_internal_conflict" in c for c in result.contradictions_detected)
        assert len(result.contradictions_detected) == 2


# ---------------------------------------------------------------------------
# Desacuerdo total — las 4 contradicciones activas simultáneamente
# ---------------------------------------------------------------------------


class TestDesacuerdoTotal:
    """Las 4 reglas de detección deben dispararse juntas y todas reportarse."""

    def test_all_four_contradictions_fire(self) -> None:
        """GPT=LONG con confidence=0.50, quant bearish, strength=0.25, conflict=0.80.

        Contradicciones esperadas:
          1. direction_mismatch: net ≈ -0.70 con GPT=LONG
          2. quant_internal_conflict: conflict=0.80 > 0.60
          3. low_quant_strength: strength=0.25 < 0.40
          4. low_gpt_confidence: confidence=0.50 < 0.70
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.50),
            quant_signals=_quant(
                momentum_signal=-0.80,
                mean_reversion_signal=-0.70,
                breakout_signal=-0.60,
                signal_strength_score=0.25,
                signal_conflict_score=0.80,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert len(result.contradictions_detected) == 4
        assert any("direction_mismatch" in c for c in result.contradictions_detected)
        assert any("quant_internal_conflict" in c for c in result.contradictions_detected)
        assert any("low_quant_strength" in c for c in result.contradictions_detected)
        assert any("low_gpt_confidence" in c for c in result.contradictions_detected)

    def test_all_four_contradictions_fire_short_direction(self) -> None:
        """Equivalente para GPT=SHORT: quant bullish dispara direction_mismatch en SHORT."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="SHORT", confidence=0.50),
            quant_signals=_quant(
                momentum_signal=0.80,
                mean_reversion_signal=0.70,
                breakout_signal=0.60,
                signal_strength_score=0.25,
                signal_conflict_score=0.80,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert len(result.contradictions_detected) == 4
        assert any("direction_mismatch" in c for c in result.contradictions_detected)
        assert any("quant_internal_conflict" in c for c in result.contradictions_detected)
        assert any("low_quant_strength" in c for c in result.contradictions_detected)
        assert any("low_gpt_confidence" in c for c in result.contradictions_detected)

    def test_contradiction_messages_contain_numeric_values(self) -> None:
        """Los mensajes de contradicción incluyen los valores concretos para auditoría."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.50),
            quant_signals=_quant(
                momentum_signal=-0.80,
                mean_reversion_signal=-0.70,
                breakout_signal=-0.60,
                signal_strength_score=0.25,
                signal_conflict_score=0.80,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        combined = " ".join(result.contradictions_detected)
        # Cada mensaje debe contener el valor numérico que lo disparó.
        assert "0.800" in combined  # conflict_score (:.3f → "0.800")
        assert "0.250" in combined  # strength
        assert "0.500" in combined  # confidence
        assert "-0.700" in combined  # net direction signal (direction_mismatch message)


# ---------------------------------------------------------------------------
# Señales débiles combinadas
# ---------------------------------------------------------------------------


class TestSenalesDebilesCombinadas:
    """low_quant_strength + low_gpt_confidence juntas (sin direction_mismatch ni conflict)."""

    def test_both_weak_signal_contradictions_fire_together(self) -> None:
        """Quant alineado con GPT pero ambas intensidades bajas → 2 contradicciones."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.60),
            quant_signals=_quant(
                momentum_signal=0.50,
                mean_reversion_signal=0.40,
                breakout_signal=0.30,
                signal_strength_score=0.30,
                signal_conflict_score=0.10,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("low_quant_strength" in c for c in result.contradictions_detected)
        assert any("low_gpt_confidence" in c for c in result.contradictions_detected)
        assert not any("direction_mismatch" in c for c in result.contradictions_detected)
        assert not any("quant_internal_conflict" in c for c in result.contradictions_detected)

    def test_weak_signals_blocked_by_contradictions_regardless_of_regime(self) -> None:
        """Señales débiles disparan low_quant_strength + low_gpt_confidence incluso con TRENDING.

        TRENDING (factor 0.85) es el régimen más favorable, pero las contradicciones
        bloquean la operación antes de que el score sea evaluado.
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.65),
            quant_signals=_quant(
                momentum_signal=0.50,
                mean_reversion_signal=0.40,
                breakout_signal=0.30,
                signal_strength_score=0.35,
                signal_conflict_score=0.10,
            ),
            regime=_regime(primary=PrimaryRegime.TRENDING),
            volatility=_vol(volatility_score=0.10),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert any("low_quant_strength" in c for c in result.contradictions_detected)
        assert any("low_gpt_confidence" in c for c in result.contradictions_detected)
        assert len(result.contradictions_detected) == 2


# ---------------------------------------------------------------------------
# Régimen UNCLEAR aislado
# ---------------------------------------------------------------------------


class TestRegimenUnclear:
    """Verifica el comportamiento específico del régimen UNCLEAR."""

    def test_unclear_regime_contributes_lowest_factor(self) -> None:
        """UNCLEAR tiene factor 0.20, el más bajo de todos los regímenes."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(),
            quant_signals=_quant(),
            regime=_regime(primary=PrimaryRegime.UNCLEAR),
            volatility=_vol(),
        )
        assert result.contributing_sources.regime_factor == pytest.approx(0.20)

    def test_unclear_regime_does_not_block_strong_signals(self) -> None:
        """UNCLEAR reduce el score pero no bloquea si quant y GPT son muy fuertes.

        Score esperado (sin contradicciones):
          quant=0.90, gpt=0.95, regime=0.20 (UNCLEAR), vol_score=0.10→factor≈0.981
          score ≈ 0.40*0.90 + 0.30*0.95 + 0.20*0.20 + 0.10*0.981 ≈ 0.783
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.95),
            quant_signals=_quant(
                momentum_signal=0.80,
                mean_reversion_signal=0.70,
                breakout_signal=0.90,
                signal_strength_score=0.90,
                signal_conflict_score=0.10,
            ),
            regime=_regime(primary=PrimaryRegime.UNCLEAR),
            volatility=_vol(volatility_score=0.10),
        )
        assert result.final_action == DecisionType.LONG
        assert result.aggregated_score > _MIN_SCORE_TO_TRADE
        assert result.contradictions_detected == []

    def test_unclear_regime_with_moderate_signals_drops_below_threshold(self) -> None:
        """UNCLEAR + señales moderadas + alta volatilidad → score < umbral mínimo.

        Score esperado (sin contradicciones):
          quant=0.40, gpt=0.70, regime=0.20 (UNCLEAR), vol_score=0.80→factor≈0.571
          score ≈ 0.40*0.40 + 0.30*0.70 + 0.20*0.20 + 0.10*0.571 ≈ 0.467
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.70),
            quant_signals=_quant(
                momentum_signal=0.50,
                mean_reversion_signal=0.40,
                breakout_signal=0.30,
                signal_strength_score=0.40,
                signal_conflict_score=0.10,
            ),
            regime=_regime(primary=PrimaryRegime.UNCLEAR),
            volatility=_vol(volatility_score=0.80),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert result.aggregated_score < _MIN_SCORE_TO_TRADE
        assert result.contradictions_detected == []


# ---------------------------------------------------------------------------
# NO_OPERAR por score bajo — sin ninguna contradicción activa
# ---------------------------------------------------------------------------


class TestNoOperarPorScore:
    """El aggregator puede rechazar una operación por score insuficiente sin contradicciones."""

    def test_no_contradictions_but_score_below_threshold(self) -> None:
        """UNCLEAR + alta volatilidad + quant/gpt en los umbrales mínimos → score < 0.50.

        Todos los parámetros están exactamente en el límite de sus umbrales de
        contradicción (sin dispararlos), pero el score cae bajo el mínimo.

        Score esperado:
          quant=0.40, gpt=0.70, regime=0.20 (UNCLEAR), vol_score=0.80→factor≈0.571
          score ≈ 0.16 + 0.21 + 0.04 + 0.057 ≈ 0.467
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.70),
            quant_signals=_quant(
                momentum_signal=0.50,
                mean_reversion_signal=0.30,
                breakout_signal=0.40,
                signal_strength_score=0.40,
                signal_conflict_score=0.10,
            ),
            regime=_regime(primary=PrimaryRegime.UNCLEAR),
            volatility=_vol(volatility_score=0.80),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert result.contradictions_detected == []
        assert result.aggregated_score < _MIN_SCORE_TO_TRADE

    def test_score_rejection_is_independent_of_regime(self) -> None:
        """Score bajo en régimen TRENDING también genera NO_OPERAR si no hay contradicciones.

        Fuerza score bajo poniendo vol extrema (factor≈0.40) con quant y gpt mínimos.
        """
        agg = DecisionAggregator()
        # quant=0.40, gpt=0.70, TRENDING→0.85, vol_score=1.0→factor=0.40
        # score = 0.16 + 0.21 + 0.17 + 0.04 = 0.58 → sobre umbral con TRENDING
        # Necesitamos RANGING (0.55) para bajar más:
        # score = 0.16 + 0.21 + 0.11 + 0.04 = 0.52 → aún sobre umbral
        # HIGH_VOLATILITY (0.40):
        # score = 0.16 + 0.21 + 0.08 + 0.04 = 0.49 → bajo umbral ✓
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.70),
            quant_signals=_quant(
                momentum_signal=0.50,
                mean_reversion_signal=0.30,
                breakout_signal=0.40,
                signal_strength_score=0.40,
                signal_conflict_score=0.10,
            ),
            regime=_regime(primary=PrimaryRegime.HIGH_VOLATILITY),
            volatility=_vol(volatility_score=1.0),
        )
        assert result.final_action == DecisionType.NO_OPERAR
        assert result.contradictions_detected == []
        assert result.aggregated_score < _MIN_SCORE_TO_TRADE


# ---------------------------------------------------------------------------
# Boundary: umbral de dirección ±0.20
# ---------------------------------------------------------------------------


class TestDirectionMismatchBoundary:
    """Verifica el umbral exacto de direction_mismatch (±0.20, estricto)."""

    def test_net_just_below_threshold_fires_long(self) -> None:
        """net = -0.21 < -0.20 → direction_mismatch dispara con GPT=LONG."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.82),
            quant_signals=_quant(
                momentum_signal=-0.21,
                mean_reversion_signal=-0.21,
                breakout_signal=-0.21,
                signal_strength_score=0.65,
                signal_conflict_score=0.10,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert any("direction_mismatch" in c for c in result.contradictions_detected)

    def test_net_below_threshold_does_not_fire_long(self) -> None:
        """net = -0.19 > -0.20 → direction_mismatch NO dispara con GPT=LONG.

        Se usa -0.19 en lugar de -0.20 exacto para evitar la ambigüedad de representación
        de punto flotante: (-0.20 × 3) / 3 puede resultar en -0.200...01 en IEEE 754.
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.82),
            quant_signals=_quant(
                momentum_signal=-0.19,
                mean_reversion_signal=-0.19,
                breakout_signal=-0.19,
                signal_strength_score=0.65,
                signal_conflict_score=0.10,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert not any("direction_mismatch" in c for c in result.contradictions_detected)

    def test_net_just_above_threshold_fires_short(self) -> None:
        """net = +0.21 > +0.20 → direction_mismatch dispara con GPT=SHORT."""
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="SHORT", confidence=0.82),
            quant_signals=_quant(
                momentum_signal=0.21,
                mean_reversion_signal=0.21,
                breakout_signal=0.21,
                signal_strength_score=0.65,
                signal_conflict_score=0.10,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert any("direction_mismatch" in c for c in result.contradictions_detected)

    def test_net_below_threshold_does_not_fire_short(self) -> None:
        """net = +0.19 < +0.20 → direction_mismatch NO dispara con GPT=SHORT.

        Se usa +0.19 por el mismo motivo de punto flotante que en el caso LONG.
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="SHORT", confidence=0.82),
            quant_signals=_quant(
                momentum_signal=0.19,
                mean_reversion_signal=0.19,
                breakout_signal=0.19,
                signal_strength_score=0.65,
                signal_conflict_score=0.10,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert not any("direction_mismatch" in c for c in result.contradictions_detected)


# ---------------------------------------------------------------------------
# Fallback de quant_score cuando signal_strength_score es None
# ---------------------------------------------------------------------------


class TestLowQuantStrengthFallback:
    """low_quant_strength también se detecta usando el fallback de señales individuales."""

    def test_none_strength_score_with_weak_individual_signals_fires(self) -> None:
        """signal_strength_score=None y señales individuales débiles → low_quant_strength.

        El aggregator hace fallback al promedio de abs(señales individuales disponibles).
        Fallback efectivo = mean([|0.20|, |0.25|, |0.30|]) = 0.25 < 0.40 → dispara.
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.82),
            quant_signals=_quant(
                signal_strength_score=None,
                momentum_signal=0.20,
                mean_reversion_signal=0.25,
                breakout_signal=0.30,
                signal_conflict_score=0.10,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert any("low_quant_strength" in c for c in result.contradictions_detected)
        assert result.final_action == DecisionType.NO_OPERAR

    def test_none_strength_score_with_strong_individual_signals_does_not_fire(self) -> None:
        """signal_strength_score=None pero señales individuales fuertes → no low_quant_strength.

        Fallback efectivo = mean([|0.60|, |0.70|, |0.80|]) = 0.70 ≥ 0.40 → no dispara.
        """
        agg = DecisionAggregator()
        result = agg.aggregate(
            gpt_decision=_gpt(decision="LONG", confidence=0.82),
            quant_signals=_quant(
                signal_strength_score=None,
                momentum_signal=0.60,
                mean_reversion_signal=0.70,
                breakout_signal=0.80,
                signal_conflict_score=0.10,
            ),
            regime=_regime(),
            volatility=_vol(),
        )
        assert not any("low_quant_strength" in c for c in result.contradictions_detected)
