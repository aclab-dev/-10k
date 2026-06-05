"""Tests exhaustivos del Risk Engine — gaps de cobertura F9 [76].

Complementa los archivos de tests existentes cubriendo:
- Engine con decisiones SHORT (APPROVE / BLOCK / ADJUST_DOWN)
- Engine con entornos TESTNET y LIVE (config.execution.environment)
- Engine BLOCK vía sl_required y tp_or_exit_plan
- Todos los símbolos permitidos atravesando el engine
- to_db_kwargs para decisión NO_OPERAR
- RiskValidationResult con original_margin_usdt=0 en NO_OPERAR
"""

from __future__ import annotations

import uuid
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
from backend.risk_engine.engine import validate
from backend.risk_engine.schemas import AdjustedParameters, RiskDecision, RiskValidationResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _bypass_validation(decision: ModelDecision, **overrides: object) -> ModelDecision:
    """Crea una copia de ModelDecision eludiendo todos los validadores de Pydantic.

    Usar ÚNICAMENTE en tests de defensa en profundidad (guard-failure scenario):
    simula qué ocurre si el JSON Schema Guard falla upstream y el engine recibe
    un objeto con datos inválidos que nunca podría producir model_validate().

    Implementación: model_construct() es la API oficial de Pydantic v2 para
    construir instancias sin ejecutar field validators ni model validators.
    """
    fields = {f: getattr(decision, f) for f in ModelDecision.model_fields}
    fields.update(overrides)
    return ModelDecision.model_construct(**fields)


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


def _long_decision(symbol: str = "BTCUSDT", **overrides: object) -> ModelDecision:
    """LONG ejecutable válido en PAPER con parámetros conservadores."""
    base: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": _now().isoformat(),
        "decision": "LONG",
        "symbol": symbol,
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


def _short_decision(symbol: str = "BTCUSDT", **overrides: object) -> ModelDecision:
    """SHORT ejecutable válido en PAPER.

    entry=95000, SL=100000 (sobre entry), TP=85000 (bajo entry).
    liq_dist=18% → liq=95000*1.18=112100; SL=100000 < liq → OK.
    """
    base: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": _now().isoformat(),
        "decision": "SHORT",
        "symbol": symbol,
        "entry_type": "MARKET",
        "entry_price": 95000.0,
        "stop_loss": 100000.0,
        "take_profit": 85000.0,
        "invalidation_price": 101000.0,
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
        "confidence": 0.80,
        "market_regime": PrimaryRegime.TRENDING.value,
        "setup_name": "breakdown",
        "timeframes_used": ["15m", "1h"],
        "quant_signals": _quant_signals().model_dump(),
        "decision_aggregator": _aggregator_section().model_dump(),
        "news_context": _news().model_dump(),
        "position_management_plan": _position_plan().model_dump(),
        "decision_rationale_summary": "Breakdown with confirmed momentum.",
        "execute": True,
    }
    base.update(overrides)
    return ModelDecision.model_validate(base)


def _no_operar_decision(symbol: str = "BTCUSDT") -> ModelDecision:
    """NO_OPERAR con execute=False."""
    base: dict[str, object] = {
        "environment": "PAPER",
        "timestamp_utc": _now().isoformat(),
        "decision": "NO_OPERAR",
        "symbol": symbol,
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
    return ModelDecision.model_validate(base)


def _aggregation(decision: ModelDecision) -> DecisionAggregationResult:
    action = DecisionType.NO_OPERAR if not decision.execute else decision.decision
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
        final_action=action,
    )


def _config() -> AppConfig:
    return get_config()


def _config_for_env(env: Environment) -> AppConfig:
    """Config con el entorno de ejecución explícito."""
    cfg = get_config()
    custom_exec = cfg.execution.model_copy(update={"environment": env})
    return cfg.model_copy(update={"execution": custom_exec})


def _config_with_margin_cap(cap: float) -> AppConfig:
    cfg = get_config()
    custom_risk = cfg.risk.model_copy(update={"max_margin_per_trade_usdt": cap})
    return cfg.model_copy(update={"risk": custom_risk})


# ---------------------------------------------------------------------------
# Engine — SHORT APPROVE
# ---------------------------------------------------------------------------


class TestRiskEngineShortApprove:
    def test_short_approves_when_all_checks_pass(self) -> None:
        decision = _short_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), _config())
        assert result.decision == RiskDecision.APPROVE

    def test_short_approve_preserves_symbol(self) -> None:
        decision = _short_decision(symbol="ETHUSDT")
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), _config())
        assert result.symbol == "ETHUSDT"

    def test_short_approve_has_adjusted_parameters(self) -> None:
        decision = _short_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), _config())
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.margin_usdt == Decimal("5.0")
        assert result.adjusted_parameters.leverage == 5

    def test_short_approve_preserves_original_parameters(self) -> None:
        decision = _short_decision(margin_usdt=5.0, leverage=5)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), _config())
        assert result.original_margin_usdt == Decimal("5.0")
        assert result.original_leverage == 5

    def test_short_approve_includes_liquidation_safety_in_reasons(self) -> None:
        decision = _short_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), _config())
        assert "liquidation_safety" in result.reasons

    def test_short_approve_near_daily_drawdown_limit(self) -> None:
        """9.9% pérdida diaria < 10% límite → SHORT igual se aprueba."""
        cfg = _config()
        decision = _short_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("9.9"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE


# ---------------------------------------------------------------------------
# Engine — SHORT BLOCK
# ---------------------------------------------------------------------------


class TestRiskEngineShortBlock:
    def test_short_blocks_on_daily_drawdown(self) -> None:
        cfg = _config()
        decision = _short_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert "daily_drawdown" in result.reasons

    def test_short_blocks_on_total_drawdown(self) -> None:
        cfg = _config()
        decision = _short_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("50.0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert "total_drawdown" in result.reasons

    def test_short_block_has_no_adjusted_parameters(self) -> None:
        cfg = _config()
        decision = _short_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert result.adjusted_parameters is None

    def test_short_block_records_loss_snapshot(self) -> None:
        cfg = _config()
        decision = _short_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("30.0"), cfg)
        assert result.daily_loss_at_check_usdt == Decimal("10.0")
        assert result.total_loss_at_check_usdt == Decimal("30.0")

    def test_short_block_via_liquidation_safety(self) -> None:
        """SHORT con SL más allá de la liquidación → BLOCK."""
        cfg = _config()
        # entry=95000, liq_dist=18% → liq=112100; SL=115000 > liq → BLOCK
        decision = _short_decision(stop_loss=115000.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert "sl_after_liquidation" in result.reasons


# ---------------------------------------------------------------------------
# Engine — SHORT ADJUST_DOWN
# ---------------------------------------------------------------------------


class TestRiskEngineShortAdjustDown:
    def test_short_adjust_down_when_margin_exceeds_cap(self) -> None:
        cfg = _config_with_margin_cap(3.0)
        decision = _short_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.ADJUST_DOWN
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.margin_usdt == Decimal("3.0")

    def test_short_adjust_down_preserves_original_margin(self) -> None:
        cfg = _config_with_margin_cap(3.0)
        decision = _short_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.original_margin_usdt == Decimal("5.0")

    def test_short_block_has_precedence_over_adjust_down(self) -> None:
        """Drawdown bloqueante tiene precedencia sobre el ajuste de margen."""
        cfg = _config_with_margin_cap(3.0)
        decision = _short_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK


# ---------------------------------------------------------------------------
# Engine — Entornos TESTNET y LIVE
# ---------------------------------------------------------------------------


class TestRiskEngineTestnet:
    def test_testnet_approves_within_leverage_cap(self) -> None:
        cfg = _config_for_env(Environment.TESTNET)
        testnet_cap = cfg.leverage.max_leverage_testnet
        # Usar leverage en o bajo el cap de TESTNET
        decision = _long_decision(leverage=min(testnet_cap, 5))
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE

    def test_testnet_adjust_down_when_leverage_exceeds_cap(self) -> None:
        cfg = _config_for_env(Environment.TESTNET)
        testnet_cap = cfg.leverage.max_leverage_testnet
        paper_cap = cfg.leverage.max_leverage_paper
        # Solo ejecutar si TESTNET tiene cap menor que PAPER
        if testnet_cap >= paper_cap:
            pytest.skip("TESTNET cap >= PAPER cap en esta config")
        decision = _long_decision(leverage=testnet_cap + 1)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.ADJUST_DOWN
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.leverage == testnet_cap

    def test_testnet_block_on_drawdown(self) -> None:
        cfg = _config_for_env(Environment.TESTNET)
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK

    def test_testnet_result_has_correct_symbol(self) -> None:
        cfg = _config_for_env(Environment.TESTNET)
        decision = _long_decision(symbol="SOLUSDT")
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.symbol == "SOLUSDT"


class TestRiskEngineLive:
    def test_live_approves_within_absolute_leverage_cap(self) -> None:
        cfg = _config_for_env(Environment.LIVE)
        live_cap = cfg.leverage.max_leverage_live_absolute
        decision = _long_decision(leverage=min(live_cap, 5))
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE

    def test_live_adjust_down_when_leverage_exceeds_absolute_cap(self) -> None:
        cfg = _config_for_env(Environment.LIVE)
        live_cap = cfg.leverage.max_leverage_live_absolute
        decision = _long_decision(leverage=live_cap + 1)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.ADJUST_DOWN
        assert result.adjusted_parameters is not None
        assert result.adjusted_parameters.leverage == live_cap

    def test_live_block_on_total_drawdown(self) -> None:
        cfg = _config_for_env(Environment.LIVE)
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("50.0"), cfg)
        assert result.decision == RiskDecision.BLOCK

    def test_live_no_operar_propagates_without_evaluating_risk(self) -> None:
        cfg = _config_for_env(Environment.LIVE)
        decision = _no_operar_decision()
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            daily_loss_usdt=Decimal(str(cfg.challenge.initial_balance_usdt)),
            total_loss_usdt=Decimal(str(cfg.challenge.initial_balance_usdt)),
            config=cfg,
        )
        assert result.decision == RiskDecision.NO_OPERAR


# ---------------------------------------------------------------------------
# Engine — BLOCK vía sl_required (SL=0 + execute=True)
# ---------------------------------------------------------------------------


class TestRiskEngineBlockViaSlRequired:
    """Tests de defensa en profundidad: engine con stop_loss=0 y execute=True.

    ModelDecision.model_validate() lanza ValidationError para este estado, por lo
    que es inalcanzable en el flujo normal de producción. Estos tests verifican
    que el engine rechaza correctamente la operación aunque el JSON Schema Guard
    falle upstream. Se usa _bypass_validation() para construir el escenario.
    """

    def test_block_when_sl_is_zero_and_execute_true(self) -> None:
        cfg = _config()
        # Guard-failure scenario: stop_loss=0 nunca llega aquí en producción.
        decision = _bypass_validation(_long_decision(), stop_loss=0.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert "sl_required" in result.reasons

    def test_sl_required_block_has_no_adjusted_parameters(self) -> None:
        cfg = _config()
        decision = _bypass_validation(_long_decision(), stop_loss=0.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.adjusted_parameters is None

    def test_sl_required_block_includes_all_check_reasons_for_audit(self) -> None:
        """Auditoría: el engine no hace short-circuit por diseño.

        Evalúa todos los checks y acumula sus resultados en reasons para tener
        trazabilidad completa de auditoría — contrato garantizado por diseño.
        """
        cfg = _config()
        decision = _bypass_validation(_long_decision(), stop_loss=0.0)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert "sl_required" in result.reasons
        assert "tp_or_exit_plan" in result.reasons
        assert "daily_drawdown" in result.reasons
        assert "total_drawdown" in result.reasons


# ---------------------------------------------------------------------------
# Engine — BLOCK vía tp_or_exit_plan
# ---------------------------------------------------------------------------


class TestRiskEngineBlockViaTpOrExitPlan:
    """Tests de defensa en profundidad: engine con take_profit=0 y execute=True.

    ModelDecision valida take_profit > entry_price para LONG (e inverso para SHORT),
    por lo que take_profit=0 es inalcanzable en producción vía model_validate.
    Estos tests verifican que el engine aplica su propio check tp_or_exit_plan
    aunque el JSON Schema Guard falle upstream. Se usa _bypass_validation().
    """

    def test_block_when_no_tp_and_no_exit_plan(self) -> None:
        cfg = _config()
        plan = _position_plan(
            use_trailing_stop=False,
            move_to_break_even=False,
            partial_close_plan="",
            max_time_in_trade_minutes=0,
        )
        # Guard-failure scenario: take_profit=0 nunca llega aquí en producción.
        decision = _bypass_validation(
            _long_decision(position_management_plan=plan.model_dump()),
            take_profit=0.0,
        )
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert "tp_or_exit_plan" in result.reasons

    def test_tp_exit_plan_block_has_no_adjusted_parameters(self) -> None:
        cfg = _config()
        plan = _position_plan(
            use_trailing_stop=False,
            move_to_break_even=False,
            partial_close_plan="",
            max_time_in_trade_minutes=0,
        )
        decision = _bypass_validation(
            _long_decision(position_management_plan=plan.model_dump()),
            take_profit=0.0,
        )
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.adjusted_parameters is None

    def test_approve_when_only_trailing_stop_present(self) -> None:
        """Trailing stop sin TP explícito es suficiente plan de salida → APPROVE."""
        cfg = _config()
        plan = _position_plan(
            use_trailing_stop=True,
            move_to_break_even=False,
            partial_close_plan="",
            max_time_in_trade_minutes=0,
        )
        decision = _bypass_validation(
            _long_decision(position_management_plan=plan.model_dump()),
            take_profit=0.0,
        )
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE

    def test_approve_when_only_time_limit_present(self) -> None:
        """Límite temporal sin TP explícito es suficiente plan de salida → APPROVE."""
        cfg = _config()
        plan = _position_plan(
            use_trailing_stop=False,
            move_to_break_even=False,
            partial_close_plan="",
            max_time_in_trade_minutes=60,
        )
        decision = _bypass_validation(
            _long_decision(position_management_plan=plan.model_dump()),
            take_profit=0.0,
        )
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE


# ---------------------------------------------------------------------------
# Engine — todos los símbolos permitidos
# ---------------------------------------------------------------------------


ALLOWED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]


class TestRiskEngineAllSymbols:
    @pytest.mark.parametrize("symbol", ALLOWED_SYMBOLS)
    def test_approve_for_each_allowed_symbol(self, symbol: str) -> None:
        cfg = _config()
        decision = _long_decision(symbol=symbol)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.APPROVE
        assert result.symbol == symbol

    @pytest.mark.parametrize("symbol", ALLOWED_SYMBOLS)
    def test_block_for_each_allowed_symbol_on_drawdown(self, symbol: str) -> None:
        cfg = _config()
        decision = _long_decision(symbol=symbol)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("10.0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.BLOCK
        assert result.symbol == symbol

    @pytest.mark.parametrize("symbol", ALLOWED_SYMBOLS)
    def test_no_operar_for_each_allowed_symbol(self, symbol: str) -> None:
        cfg = _config()
        decision = _no_operar_decision(symbol=symbol)
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("0"), Decimal("0"), cfg)
        assert result.decision == RiskDecision.NO_OPERAR
        assert result.symbol == symbol


# ---------------------------------------------------------------------------
# to_db_kwargs — decisión NO_OPERAR
# ---------------------------------------------------------------------------


class TestToDbKwargsNoOperar:
    def _build_no_operar_result(self) -> RiskValidationResult:
        return RiskValidationResult(
            aggregation_id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            timestamp_utc=_now(),
            decision=RiskDecision.NO_OPERAR,
            original_margin_usdt=Decimal("0"),
            original_leverage=1,
            adjusted_parameters=None,
            daily_loss_at_check_usdt=Decimal("5.0"),
            total_loss_at_check_usdt=Decimal("20.0"),
            reasons={"no_operar": "Sin edge."},
        )

    def test_no_operar_result_is_no_operar_string(self) -> None:
        result = self._build_no_operar_result()
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["result"] == "NO_OPERAR"

    def test_no_operar_adjusted_margin_is_none(self) -> None:
        result = self._build_no_operar_result()
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["adjusted_margin"] is None

    def test_no_operar_adjusted_leverage_is_none(self) -> None:
        result = self._build_no_operar_result()
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["adjusted_leverage"] is None

    def test_no_operar_original_margin_mapped(self) -> None:
        result = self._build_no_operar_result()
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["original_margin"] == Decimal("0")

    def test_no_operar_loss_fields_mapped(self) -> None:
        result = self._build_no_operar_result()
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["daily_loss_at_check"] == Decimal("5.0")
        assert kwargs["total_loss_at_check"] == Decimal("20.0")

    def test_no_operar_reasons_mapped(self) -> None:
        result = self._build_no_operar_result()
        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["reasons"] == {"no_operar": "Sin edge."}

    def test_no_operar_symbol_mapped(self) -> None:
        result = self._build_no_operar_result()
        bot_run_id = str(uuid.uuid4())
        kwargs = result.to_db_kwargs(bot_run_id)
        assert kwargs["symbol"] == "BTCUSDT"
        assert kwargs["bot_run_id"] == bot_run_id

    def test_no_operar_full_kwargs_via_engine(self) -> None:
        """Integración: resultado del engine → to_db_kwargs para NO_OPERAR."""
        cfg = _config()
        decision = _no_operar_decision()
        aggregation = _aggregation(decision)
        result = validate(aggregation, decision, Decimal("3.0"), Decimal("10.0"), cfg)
        assert result.decision == RiskDecision.NO_OPERAR

        kwargs = result.to_db_kwargs(str(uuid.uuid4()))
        assert kwargs["result"] == "NO_OPERAR"
        assert kwargs["adjusted_margin"] is None
        assert kwargs["adjusted_leverage"] is None


# ---------------------------------------------------------------------------
# RiskValidationResult — original_margin_usdt=0 permitido en NO_OPERAR
# ---------------------------------------------------------------------------


class TestRiskValidationResultNoOperarMarginZero:
    def test_no_operar_with_zero_margin_is_valid(self) -> None:
        """NO_OPERAR puede tener margin=0 porque no hay trade que ejecutar."""
        result = RiskValidationResult(
            aggregation_id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            timestamp_utc=_now(),
            decision=RiskDecision.NO_OPERAR,
            original_margin_usdt=Decimal("0"),
            original_leverage=1,
            adjusted_parameters=None,
            reasons={"no_operar": "Sin edge."},
        )
        assert result.original_margin_usdt == Decimal("0")
        assert result.decision == RiskDecision.NO_OPERAR

    def test_no_operar_with_nonzero_margin_also_valid(self) -> None:
        """NO_OPERAR con margen > 0 también es válido (e.g. señal con contexto)."""
        result = RiskValidationResult(
            aggregation_id=str(uuid.uuid4()),
            symbol="ETHUSDT",
            timestamp_utc=_now(),
            decision=RiskDecision.NO_OPERAR,
            original_margin_usdt=Decimal("5.0"),
            original_leverage=3,
            adjusted_parameters=None,
            reasons={"no_operar": "Aggregator sin edge."},
        )
        assert result.original_margin_usdt == Decimal("5.0")

    def test_approve_with_zero_margin_is_rejected(self) -> None:
        """APPROVE no puede tener margin=0 — requiere trade real."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RiskValidationResult(
                aggregation_id=str(uuid.uuid4()),
                symbol="BTCUSDT",
                timestamp_utc=_now(),
                decision=RiskDecision.APPROVE,
                original_margin_usdt=Decimal("0"),
                original_leverage=5,
                adjusted_parameters=AdjustedParameters(
                    margin_usdt=Decimal("5.0"),
                    leverage=5,
                ),
                reasons={"sl_required": "ok"},
            )

    def test_block_with_zero_margin_is_rejected(self) -> None:
        """BLOCK no puede tener margin=0 — implica trade ejecutable rechazado."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RiskValidationResult(
                aggregation_id=str(uuid.uuid4()),
                symbol="BTCUSDT",
                timestamp_utc=_now(),
                decision=RiskDecision.BLOCK,
                original_margin_usdt=Decimal("0"),
                original_leverage=5,
                adjusted_parameters=None,
                reasons={"daily_drawdown": "superado"},
            )


# ---------------------------------------------------------------------------
# Tests de pipeline: anti-martingala y anti-averaging en engine.validate()
# ---------------------------------------------------------------------------


class TestAntiMartingalaAntiAveragingPipeline:
    """Valida que engine.validate() bloquee patrones martingala y averaging."""

    def test_engine_blocks_martingala(self) -> None:
        """BLOCK cuando el margen propuesto supera el del último trade perdedor."""
        cfg = _config()
        decision = _long_decision(margin_usdt=8.0)
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            last_trade_pnl_usdt=Decimal("-3.0"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.decision == RiskDecision.BLOCK
        assert "anti_martingala" in result.reasons

    def test_engine_approves_without_martingala_history(self) -> None:
        """APPROVE cuando no hay historial de trades previos."""
        cfg = _config()
        decision = _long_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            last_trade_pnl_usdt=None,
            last_trade_margin_usdt=None,
        )
        assert result.decision == RiskDecision.APPROVE

    def test_engine_approves_same_margin_after_loss(self) -> None:
        """APPROVE cuando el margen propuesto es igual al del último trade perdedor."""
        cfg = _config()
        decision = _long_decision(margin_usdt=5.0)
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            last_trade_pnl_usdt=Decimal("-2.0"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.decision == RiskDecision.APPROVE

    def test_engine_blocks_averaging_on_losing_position(self) -> None:
        """BLOCK cuando hay una posición abierta con PnL no realizado negativo."""
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            open_position_unrealized_pnl_usdt=Decimal("-4.50"),
        )
        assert result.decision == RiskDecision.BLOCK
        assert "anti_averaging" in result.reasons

    def test_engine_approves_when_position_in_profit(self) -> None:
        """APPROVE cuando la posición abierta está en ganancia."""
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            open_position_unrealized_pnl_usdt=Decimal("2.0"),
        )
        assert result.decision == RiskDecision.APPROVE

    def test_engine_approves_when_no_open_position(self) -> None:
        """APPROVE cuando no hay posición abierta (None)."""
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            open_position_unrealized_pnl_usdt=None,
        )
        assert result.decision == RiskDecision.APPROVE

    def test_engine_blocks_both_simultaneously(self) -> None:
        """BLOCK con ambas reglas fallando a la vez; ambas aparecen en reasons."""
        cfg = _config()
        decision = _long_decision(margin_usdt=8.0)
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            last_trade_pnl_usdt=Decimal("-3.0"),
            last_trade_margin_usdt=Decimal("5.0"),
            open_position_unrealized_pnl_usdt=Decimal("-2.0"),
        )
        assert result.decision == RiskDecision.BLOCK
        assert "anti_martingala" in result.reasons
        assert "anti_averaging" in result.reasons

    def test_engine_anti_martingala_reason_contains_values(self) -> None:
        """El reason de anti_martingala incluye los valores relevantes."""
        cfg = _config()
        decision = _long_decision(margin_usdt=8.0)
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            last_trade_pnl_usdt=Decimal("-3.0"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        reason = result.reasons["anti_martingala"]
        assert "8" in reason
        assert "5" in reason
        assert "-3" in reason

    def test_engine_anti_averaging_reason_contains_pnl(self) -> None:
        """El reason de anti_averaging incluye el PnL de la posición."""
        cfg = _config()
        decision = _long_decision()
        aggregation = _aggregation(decision)
        result = validate(
            aggregation,
            decision,
            Decimal("0"),
            Decimal("0"),
            cfg,
            open_position_unrealized_pnl_usdt=Decimal("-4.50"),
        )
        assert "-4.50" in result.reasons["anti_averaging"]
