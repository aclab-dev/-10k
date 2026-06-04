"""Tests unitarios — Separación NO_OPERAR vs BLOCKED (tarjeta [75] F9).

Verifica que:
- NO_OPERAR (sin edge, decisión del Aggregator) y BLOCKED (rechazado por Risk
  Engine) son estados distintos en RiskDecision y en el pipeline completo.
- validate() propaga NO_OPERAR cuando execute=False sin evaluar reglas de riesgo.
- validate() emite BLOCK cuando execute=True y una regla de riesgo falla.
- Ambos estados son auditables (reasons no vacío, adjusted_parameters=None).
- Los estados no son intercambiables ni equivalentes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.core.config import AppConfig, get_config
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
from backend.risk_engine.engine import validate
from backend.risk_engine.schemas import RiskDecision, RiskValidationResult

# ---------------------------------------------------------------------------
# Helpers de fixture
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


def _position_plan() -> PositionManagementPlan:
    return PositionManagementPlan(
        use_trailing_stop=True,
        move_to_break_even=True,
        partial_close_plan="none",
        max_time_in_trade_minutes=120,
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


def _no_operar_decision(**overrides: object) -> ModelDecision:
    """NO_OPERAR con execute=False — sin edge según el Aggregator."""
    base: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": _now().isoformat(),
        "decision": "NO_OPERAR",
        "symbol": "BTCUSDT",
        "entry_type": "MARKET",
        "entry_price": 95000.0,
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
        "confidence": 0.3,
        "market_regime": PrimaryRegime.RANGING.value,
        "setup_name": "none",
        "timeframes_used": ["1h"],
        "quant_signals": _quant_signals().model_dump(),
        "decision_aggregator": _aggregator_section().model_dump(),
        "news_context": _news().model_dump(),
        "position_management_plan": _position_plan().model_dump(),
        "decision_rationale_summary": "No edge detected.",
        "execute": False,
    }
    base.update(overrides)
    return ModelDecision.model_validate(base)


def _aggregation(
    decision: ModelDecision,
    final_action: DecisionType = DecisionType.LONG,
) -> DecisionAggregationResult:
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
        final_action=final_action,
    )


def _config() -> AppConfig:
    return get_config()


# ---------------------------------------------------------------------------
# Distinción en el enum RiskDecision
# ---------------------------------------------------------------------------


class TestRiskDecisionEnum:
    def test_no_operar_and_block_are_distinct_values(self) -> None:
        assert RiskDecision.NO_OPERAR != RiskDecision.BLOCK

    def test_no_operar_string_value(self) -> None:
        assert RiskDecision.NO_OPERAR == "NO_OPERAR"

    def test_block_string_value(self) -> None:
        assert RiskDecision.BLOCK == "BLOCK"

    def test_four_decisions_exist(self) -> None:
        """El enum debe tener exactamente los cuatro estados documentados."""
        values = {d.value for d in RiskDecision}
        assert values == {"APPROVE", "ADJUST_DOWN", "BLOCK", "NO_OPERAR"}


# ---------------------------------------------------------------------------
# validate() emite NO_OPERAR cuando execute=False
# ---------------------------------------------------------------------------


class TestValidateNoOperar:
    def test_returns_no_operar_when_execute_false(self) -> None:
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()

        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

        assert result.decision == RiskDecision.NO_OPERAR

    def test_no_operar_has_no_adjusted_parameters(self) -> None:
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()

        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

        assert result.adjusted_parameters is None

    def test_no_operar_has_auditable_reasons(self) -> None:
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()

        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

        assert len(result.reasons) >= 1
        assert "no_operar" in result.reasons
        # La razón debe mencionar el origen (Aggregator) para que sea auditable
        reason = result.reasons["no_operar"]
        assert "Aggregator" in reason or "execute" in reason

    def test_no_operar_carries_aggregation_id(self) -> None:
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()

        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

        assert result.aggregation_id == aggregation.aggregation_id

    def test_no_operar_carries_symbol(self) -> None:
        decision = _no_operar_decision(symbol="ETHUSDT")
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()

        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

        assert result.symbol == "ETHUSDT"

    def test_no_operar_carries_loss_snapshots(self) -> None:
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()
        daily = Decimal("15.0")
        total = Decimal("40.0")

        result = validate(aggregation, decision, daily, total, cfg)

        assert result.daily_loss_at_check_usdt == daily
        assert result.total_loss_at_check_usdt == total

    def test_no_operar_is_valid_risk_validation_result(self) -> None:
        """El resultado debe ser un RiskValidationResult plenamente válido."""
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()

        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

        assert isinstance(result, RiskValidationResult)

    def test_no_operar_does_not_evaluate_risk_rules(self) -> None:
        """NO_OPERAR nunca llega a evaluar reglas de riesgo, incluso con pérdidas altas.

        Si el Risk Engine evaluara reglas, un drawdown del 100% generaría BLOCK.
        Pero como execute=False → debe retornar NO_OPERAR sin importar las pérdidas.
        """
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()
        # Pérdidas extremas que generarían BLOCK si se evaluaran
        initial_balance = Decimal(str(cfg.challenge.initial_balance_usdt))

        result = validate(
            aggregation,
            decision,
            daily_loss_usdt=initial_balance,  # 100% del capital diario perdido
            total_loss_usdt=initial_balance,
            config=cfg,
        )

        # Debe ser NO_OPERAR, no BLOCK
        assert result.decision == RiskDecision.NO_OPERAR


# ---------------------------------------------------------------------------
# validate() emite BLOCK cuando execute=True y hay violación de riesgo
# ---------------------------------------------------------------------------


class TestValidateBlocked:
    def test_returns_block_when_drawdown_exceeded(self) -> None:
        decision = _long_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.LONG)
        cfg = _config()
        # Pérdida diaria que excede el límite
        initial_balance = Decimal(str(cfg.challenge.initial_balance_usdt))
        daily_limit_pct = Decimal(str(cfg.risk.max_daily_loss_percent)) / 100
        exceeding_loss = initial_balance * daily_limit_pct + Decimal("1")

        result = validate(aggregation, decision, exceeding_loss, Decimal("0"), cfg)

        assert result.decision == RiskDecision.BLOCK

    def test_block_has_no_adjusted_parameters(self) -> None:
        decision = _long_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.LONG)
        cfg = _config()
        initial_balance = Decimal(str(cfg.challenge.initial_balance_usdt))
        daily_limit_pct = Decimal(str(cfg.risk.max_daily_loss_percent)) / 100
        exceeding_loss = initial_balance * daily_limit_pct + Decimal("1")

        result = validate(aggregation, decision, exceeding_loss, Decimal("0"), cfg)

        assert result.adjusted_parameters is None

    def test_block_has_auditable_reasons(self) -> None:
        decision = _long_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.LONG)
        cfg = _config()
        initial_balance = Decimal(str(cfg.challenge.initial_balance_usdt))
        daily_limit_pct = Decimal(str(cfg.risk.max_daily_loss_percent)) / 100
        exceeding_loss = initial_balance * daily_limit_pct + Decimal("1")

        result = validate(aggregation, decision, exceeding_loss, Decimal("0"), cfg)

        assert len(result.reasons) >= 1


# ---------------------------------------------------------------------------
# NO_OPERAR ≠ BLOCK: estados no intercambiables
# ---------------------------------------------------------------------------


class TestNoOperarVsBlockDistinction:
    def test_no_operar_is_not_block(self) -> None:
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()

        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

        assert result.decision is not RiskDecision.BLOCK
        assert result.decision == RiskDecision.NO_OPERAR

    def test_block_is_not_no_operar(self) -> None:
        decision = _long_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.LONG)
        cfg = _config()
        initial_balance = Decimal(str(cfg.challenge.initial_balance_usdt))
        daily_limit_pct = Decimal(str(cfg.risk.max_daily_loss_percent)) / 100
        exceeding_loss = initial_balance * daily_limit_pct + Decimal("1")

        result = validate(aggregation, decision, exceeding_loss, Decimal("0"), cfg)

        assert result.decision is not RiskDecision.NO_OPERAR
        assert result.decision == RiskDecision.BLOCK

    def test_no_operar_origin_is_aggregator_not_risk_engine(self) -> None:
        """El reason de NO_OPERAR debe indicar que la decisión viene del Aggregator."""
        decision = _no_operar_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.NO_OPERAR)
        cfg = _config()

        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)

        reason_text = result.reasons.get("no_operar", "")
        # La razón debe mencionar que viene del Aggregator (no del Risk Engine)
        assert "Aggregator" in reason_text or "execute=False" in reason_text

    def test_block_origin_is_risk_engine(self) -> None:
        """BLOCK debe contener una reason proveniente de una regla del Risk Engine."""
        decision = _long_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.LONG)
        cfg = _config()
        initial_balance = Decimal(str(cfg.challenge.initial_balance_usdt))
        daily_limit_pct = Decimal(str(cfg.risk.max_daily_loss_percent)) / 100
        exceeding_loss = initial_balance * daily_limit_pct + Decimal("1")

        result = validate(aggregation, decision, exceeding_loss, Decimal("0"), cfg)

        # La rule "daily_drawdown" debe estar en los reasons
        assert "daily_drawdown" in result.reasons

    def test_no_operar_not_in_block_reasons(self) -> None:
        """Un BLOCK no debe contener la razón 'no_operar' — son estados distintos."""
        decision = _long_decision()
        aggregation = _aggregation(decision, final_action=DecisionType.LONG)
        cfg = _config()
        initial_balance = Decimal(str(cfg.challenge.initial_balance_usdt))
        daily_limit_pct = Decimal(str(cfg.risk.max_daily_loss_percent)) / 100
        exceeding_loss = initial_balance * daily_limit_pct + Decimal("1")

        result = validate(aggregation, decision, exceeding_loss, Decimal("0"), cfg)

        assert "no_operar" not in result.reasons


# ---------------------------------------------------------------------------
# RiskValidationResult: coherencia del schema para NO_OPERAR
# ---------------------------------------------------------------------------


class TestRiskValidationResultSchemaNoOperar:
    def test_no_operar_with_adjusted_parameters_raises(self) -> None:
        """NO_OPERAR con adjusted_parameters no None debe fallar la validación."""
        from pydantic import ValidationError

        from backend.risk_engine.schemas import AdjustedParameters

        with pytest.raises(ValidationError):
            RiskValidationResult(
                aggregation_id="00000000-0000-0000-0000-000000000001",
                symbol="BTCUSDT",
                timestamp_utc=_now(),
                decision=RiskDecision.NO_OPERAR,
                original_margin_usdt=Decimal("5.0"),
                original_leverage=5,
                adjusted_parameters=AdjustedParameters(
                    margin_usdt=Decimal("5.0"),
                    leverage=5,
                ),
                reasons={"no_operar": "Sin edge."},
            )

    def test_no_operar_without_adjusted_parameters_is_valid(self) -> None:
        """NO_OPERAR sin adjusted_parameters debe ser un resultado válido."""
        result = RiskValidationResult(
            aggregation_id="00000000-0000-0000-0000-000000000001",
            symbol="BTCUSDT",
            timestamp_utc=_now(),
            decision=RiskDecision.NO_OPERAR,
            original_margin_usdt=Decimal("5.0"),
            original_leverage=5,
            adjusted_parameters=None,
            reasons={"no_operar": "Sin edge."},
        )

        assert result.decision == RiskDecision.NO_OPERAR
        assert result.adjusted_parameters is None
