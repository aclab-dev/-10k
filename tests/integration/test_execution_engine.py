"""Test de integración: ExecutionEngine wireado en Orchestrator, ciclo real (CR).

Verifica el flujo no-mockeado: `Orchestrator` arma PaperAdapter +
MarketDataCycleService (con MarketAnalysisService, F5/F6) + ExecutionEngine +
PositionManager reales — mismo wiring que corre `worker/run_worker.py`. Se
corre un ciclo de market data real primero para sembrar un VolatilityAssessment
(ATR) real, requisito de `ExecutionEngine` para registrar el trailing stop.

No hay (todavía) una fuente de decisiones en vivo (Decision Aggregator/Risk
Engine/GPT no están wireados al ciclo real — ver scope de la tarjeta [145]),
así que el "ApprovedTradePlan" se arma a mano, igual que en
tests/unit/test_e2e_paper_pipeline.py, pero se ejecuta a través del
ExecutionEngine real construido por Orchestrator (no uno armado aparte,
bypaseando el wiring) — así se prueba "en el ciclo real, no en test aislado".

Ejecutar con: pytest -m integration

Nota: tick_all()/execute_approved_plan() commitean la sesión. Por eso este
test cierra el BotRun (status STOPPED) en un finally vía orch.close(), mismo
patrón que tests/integration/test_market_data_cycle_service.py, para no
contaminar BotRunRepository.get_active() en otros tests del mismo contenedor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.core.config import Environment
from backend.decision_engine.schemas import (
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
from backend.exchange_adapters.schemas import OrderStatus
from backend.market_regime.schemas import PrimaryRegime
from backend.risk_engine.schemas import AdjustedParameters, RiskDecision, RiskValidationResult
from backend.storage.repositories.trades import OrderRepository, PositionRepository, TradeRepository
from backend.trading_core.orchestrator import Orchestrator

_NOW = datetime.now(UTC)
_SYMBOL = "BTCUSDT"


def _make_decision() -> ModelDecision:
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=_NOW,
        decision=DecisionType.LONG,
        symbol=_SYMBOL,
        entry_type=EntryType.MARKET,
        entry_price=50_100.0,
        stop_loss=49_500.0,
        take_profit=51_500.0,
        invalidation_price=49_000.0,
        leverage=3,
        margin_usdt=5.0,
        estimated_notional_usdt=15.0,
        estimated_entry_fee_usdt=0.075,
        estimated_exit_fee_usdt=0.075,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.3,
        estimated_max_loss_usdt=5.0,
        liquidation_distance_percent_estimated=15.0,
        confidence=0.85,
        market_regime=PrimaryRegime.TRENDING,
        setup_name="momentum_breakout_v1",
        timeframes_used=["5m", "15m", "1h", "4h"],
        quant_signals=QuantSignalsSection(
            momentum=MomentumInterpretation.BULLISH,
            mean_reversion=MeanReversionInterpretation.NEUTRAL,
            breakout_detection=BreakoutInterpretation.CONFIRMED,
            funding_analysis=FundingInterpretation.NEUTRAL,
            open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
            order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
            liquidity_sweep=LiquiditySweepInterpretation.NONE,
        ),
        decision_aggregator=DecisionAggregatorSection(
            quant_score=0.65,
            gpt_context_score=0.85,
            risk_quality_score=0.80,
            final_trade_quality_score=0.75,
            contradictions_detected=[],
        ),
        news_context=NewsContextSection(
            used=False,
            impact=NewsImpact.NEUTRAL,
            summary="No news data used.",
        ),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=True,
            move_to_break_even=False,
            partial_close_plan="none",
            max_time_in_trade_minutes=0,
        ),
        decision_rationale_summary="integration test fixture",
        execute=True,
    )


def _make_risk_result(decision: ModelDecision) -> RiskValidationResult:
    return RiskValidationResult(
        aggregation_id=decision.decision_id,
        symbol=decision.symbol,
        timestamp_utc=_NOW,
        decision=RiskDecision.APPROVE,
        original_margin_usdt=Decimal(str(decision.margin_usdt)),
        original_leverage=decision.leverage,
        adjusted_parameters=AdjustedParameters(
            margin_usdt=Decimal(str(decision.margin_usdt)),
            leverage=decision.leverage,
        ),
        reasons={"integration_test": "fixture aprobado a mano"},
    )


@pytest.mark.integration
class TestExecutionEngineIntegration:
    def test_execute_approved_plan_through_real_orchestrator_wiring(
        self, pg_session: Session
    ) -> None:
        orch = Orchestrator(session=pg_session)
        try:
            # Ciclo real de market data: siembra un VolatilityAssessment (ATR)
            # real para BTCUSDT, requisito de ExecutionEngine.
            orch.cycle_runner._market_data_service.tick_all()  # type: ignore[attr-defined]

            decision = _make_decision()
            risk_result = _make_risk_result(decision)

            assert orch.execution_engine is not None
            result = orch.execution_engine.execute_approved_plan(decision, risk_result)

            assert result.order_result.status == OrderStatus.FILLED
            assert result.position_registered is True
            assert result.trade_id is not None

            order_row = OrderRepository(pg_session).get_by_client_order_id(decision.decision_id)
            assert order_row is not None
            assert order_row.status == OrderStatus.FILLED.value
            assert order_row.trade_id == result.trade_id

            trade_row = TradeRepository(pg_session).list_open(orch._bot_run.id)  # type: ignore[union-attr]
            assert len(trade_row) == 1
            assert trade_row[0].id == result.trade_id
            assert trade_row[0].direction == "LONG"

            position_row = PositionRepository(pg_session).get_open_by_symbol(
                orch._bot_run.id,  # type: ignore[union-attr]
                _SYMBOL,
            )
            assert position_row is not None
            assert position_row.trade_id == result.trade_id
            assert position_row.stop_loss == Decimal(str(decision.stop_loss))
            assert position_row.take_profit == Decimal(str(decision.take_profit))

            assert orch.position_manager is not None
            config = orch.position_manager.get_config(_SYMBOL)
            assert config is not None
            assert config.stop_loss == Decimal(str(decision.stop_loss))
            assert config.take_profit == Decimal(str(decision.take_profit))
        finally:
            orch.close()

    def test_execute_approved_plan_retry_does_not_duplicate_order(
        self, pg_session: Session
    ) -> None:
        """Retry del mismo plan aprobado (mismo decision_id) no debe violar la

        UniqueConstraint de orders.client_order_id ni duplicar Trade/Position.
        """
        orch = Orchestrator(session=pg_session)
        try:
            orch.cycle_runner._market_data_service.tick_all()  # type: ignore[attr-defined]

            decision = _make_decision()
            risk_result = _make_risk_result(decision)

            assert orch.execution_engine is not None
            first = orch.execution_engine.execute_approved_plan(decision, risk_result)
            second = orch.execution_engine.execute_approved_plan(decision, risk_result)

            assert second.trade_id == first.trade_id
            assert second.order_db_id == first.order_db_id
            assert second.position_registered is True

            trade_rows = TradeRepository(pg_session).list_open(orch._bot_run.id)  # type: ignore[union-attr]
            assert len(trade_rows) == 1
        finally:
            orch.close()
