"""Test E2E del Orchestrator: ciclo completo en PAPER (CR).

Valida el ensamblaje real del Orchestrator en modo PAPER. Los componentes se
construyen mediante el mismo código que corre el worker; no hay mocks internos
(MockDataFetcher sustituye solo el exchange externo, igual que en producción
PAPER).

GPT Context / Decision Aggregator no están auto-wired en _tick() todavía
(pendiente tarjeta [145]); los inputs de decisión se arman a mano para
ejercitar los tres resultados posibles del ciclo (APPROVE / BLOCK / NO_OPERAR)
a través de las capas reales de RiskEngine y ExecutionEngine.

Ejecutar con: pytest -m integration
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import Environment, get_config
from backend.decision_engine.aggregator_schemas import (
    ContributingSources,
    DecisionAggregationResult,
)
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
from backend.market_data.schemas import ALLOWED_SYMBOLS
from backend.market_regime.schemas import PrimaryRegime
from backend.risk_engine import engine as risk_engine
from backend.risk_engine.schemas import RiskDecision
from backend.storage.models import BotRun
from backend.storage.repositories.bot import BotRunRepository
from backend.storage.repositories.snapshots import (
    MarketRegimeRepository,
    MarketSnapshotRepository,
    QuantSignalRepository,
    VolatilityAssessmentRepository,
)
from backend.storage.repositories.trades import OrderRepository, PositionRepository, TradeRepository
from backend.trading_core.orchestrator import Orchestrator

_NOW = datetime.now(UTC)
_SYMBOL = "BTCUSDT"
_SYMBOLS = list(ALLOWED_SYMBOLS)


# ---------------------------------------------------------------------------
# Fixtures de decisión
# ---------------------------------------------------------------------------


def _make_approve_decision() -> ModelDecision:
    """Decisión válida con todos los parámetros de riesgo dentro de los límites."""
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=_NOW,
        decision=DecisionType.LONG,
        symbol=_SYMBOL,
        entry_type=EntryType.MARKET,
        entry_price=50_000.0,
        stop_loss=49_000.0,
        take_profit=52_000.0,
        invalidation_price=48_500.0,
        leverage=3,
        margin_usdt=5.0,
        estimated_notional_usdt=15.0,
        estimated_entry_fee_usdt=0.075,
        estimated_exit_fee_usdt=0.075,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.0,
        estimated_max_loss_usdt=5.0,
        liquidation_distance_percent_estimated=20.0,
        confidence=0.85,
        market_regime=PrimaryRegime.TRENDING,
        setup_name="momentum_e2e_test",
        timeframes_used=["5m", "15m", "1h", "4h"],
        quant_signals=_quant_signals_section(),
        decision_aggregator=DecisionAggregatorSection(
            quant_score=0.65,
            gpt_context_score=0.85,
            risk_quality_score=0.80,
            final_trade_quality_score=0.75,
            contradictions_detected=[],
        ),
        news_context=NewsContextSection(
            used=False, impact=NewsImpact.NEUTRAL, summary="no news"
        ),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=True,
            move_to_break_even=False,
            partial_close_plan="none",
            max_time_in_trade_minutes=0,
        ),
        decision_rationale_summary="e2e test fixture — APPROVE path",
        execute=True,
    )


def _make_no_operar_decision() -> ModelDecision:
    """Decisión sin edge (execute=False) — el Risk Engine emite NO_OPERAR."""
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=_NOW,
        decision=DecisionType.NO_OPERAR,
        symbol=_SYMBOL,
        entry_type=EntryType.NO_ENTRY,
        entry_price=50_000.0,
        stop_loss=0.0,
        take_profit=0.0,
        invalidation_price=0.0,
        leverage=1,
        margin_usdt=0.0,
        estimated_notional_usdt=0.0,
        estimated_entry_fee_usdt=0.0,
        estimated_exit_fee_usdt=0.0,
        estimated_slippage_usdt=0.0,
        estimated_funding_usdt=0.0,
        net_risk_reward=0.0,
        estimated_max_loss_usdt=0.0,
        liquidation_distance_percent_estimated=0.0,
        confidence=0.40,
        market_regime=PrimaryRegime.UNCLEAR,
        setup_name="no_edge",
        timeframes_used=["1h"],
        quant_signals=_quant_signals_section(),
        decision_aggregator=DecisionAggregatorSection(
            quant_score=0.30,
            gpt_context_score=0.40,
            risk_quality_score=0.20,
            final_trade_quality_score=0.30,
            contradictions_detected=["low_quant_strength", "low_gpt_confidence"],
        ),
        news_context=NewsContextSection(
            used=False, impact=NewsImpact.NEUTRAL, summary="no news"
        ),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=False,
            move_to_break_even=False,
            partial_close_plan="none",
            max_time_in_trade_minutes=0,
        ),
        decision_rationale_summary="e2e test fixture — NO_OPERAR path",
        execute=False,
    )


def _make_block_decision() -> ModelDecision:
    """Decisión LONG válida para el schema — BLOCK lo disparará el RiskEngine
    por daily_drawdown excedido (daily_loss_usdt alto al llamar validate())."""
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=_NOW,
        decision=DecisionType.LONG,
        symbol=_SYMBOL,
        entry_type=EntryType.MARKET,
        entry_price=50_000.0,
        stop_loss=49_000.0,
        take_profit=52_000.0,
        invalidation_price=48_500.0,
        leverage=3,
        margin_usdt=5.0,
        estimated_notional_usdt=15.0,
        estimated_entry_fee_usdt=0.075,
        estimated_exit_fee_usdt=0.075,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.0,
        estimated_max_loss_usdt=5.0,
        liquidation_distance_percent_estimated=20.0,
        confidence=0.85,
        market_regime=PrimaryRegime.TRENDING,
        setup_name="momentum_drawdown_exceeded",
        timeframes_used=["1h"],
        quant_signals=_quant_signals_section(),
        decision_aggregator=DecisionAggregatorSection(
            quant_score=0.65,
            gpt_context_score=0.85,
            risk_quality_score=0.80,
            final_trade_quality_score=0.75,
            contradictions_detected=[],
        ),
        news_context=NewsContextSection(
            used=False, impact=NewsImpact.NEUTRAL, summary="no news"
        ),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=False,
            move_to_break_even=False,
            partial_close_plan="none",
            max_time_in_trade_minutes=0,
        ),
        decision_rationale_summary="e2e test fixture — BLOCK path (daily drawdown excedido)",
        execute=True,
    )


def _quant_signals_section() -> QuantSignalsSection:
    return QuantSignalsSection(
        momentum=MomentumInterpretation.BULLISH,
        mean_reversion=MeanReversionInterpretation.NEUTRAL,
        breakout_detection=BreakoutInterpretation.CONFIRMED,
        funding_analysis=FundingInterpretation.NEUTRAL,
        open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
        order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
        liquidity_sweep=LiquiditySweepInterpretation.NONE,
    )


def _make_aggregation_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOrchestratorE2E:
    def test_botrun_lifecycle(self, pg_session: Session) -> None:
        """BotRun arranca en RUNNING y queda STOPPED tras close()."""
        orch = Orchestrator(session=pg_session)
        try:
            bot_run_repo = BotRunRepository(pg_session)

            assert orch._bot_run is not None
            bot_run_id = orch._bot_run.id

            pg_session.refresh(orch._bot_run)
            assert orch._bot_run.status == "RUNNING"

            active = bot_run_repo.get_active()
            assert active is not None
            assert active.id == bot_run_id

            orch.close()

            pg_session.expire_all()
            closed = pg_session.scalars(
                select(BotRun).where(BotRun.id == bot_run_id)
            ).first()
            assert closed is not None
            assert closed.status == "STOPPED"

            assert bot_run_repo.get_active() is None
        finally:
            orch.close()  # idempotente; no-op si ya se cerró arriba

    def test_full_tick_produces_market_analysis_for_all_symbols(
        self, pg_session: Session
    ) -> None:
        """Un tick real del Orchestrator persiste MarketSnapshot + QuantSignal +
        MarketRegime + VolatilityAssessment para cada símbolo permitido.

        Valida que MarketAnalysisService está correctamente wired como hook de
        MarketDataCycleService: market data → quant signals → regime → volatility
        en un solo ciclo, sin mocks internos.
        """
        orch = Orchestrator(session=pg_session)
        try:
            mds = orch.cycle_runner._market_data_service
            assert mds is not None
            mds.tick_all()

            assert orch._bot_run is not None
            bot_run_id = orch._bot_run.id

            snap_repo = MarketSnapshotRepository(pg_session)
            quant_repo = QuantSignalRepository(pg_session)
            regime_repo = MarketRegimeRepository(pg_session)
            vol_repo = VolatilityAssessmentRepository(pg_session)

            for symbol in _SYMBOLS:
                assert snap_repo.get_latest_by_symbol(bot_run_id, symbol) is not None, (
                    f"MarketSnapshot faltante para {symbol}"
                )
                assert quant_repo.get_latest_by_symbol(bot_run_id, symbol) is not None, (
                    f"QuantSignal faltante para {symbol}"
                )
                assert regime_repo.get_latest_by_symbol(bot_run_id, symbol) is not None, (
                    f"MarketRegime faltante para {symbol}"
                )
                assert vol_repo.get_latest_by_symbol(bot_run_id, symbol) is not None, (
                    f"VolatilityAssessment faltante para {symbol}"
                )
        finally:
            orch.close()

    def test_approve_outcome_executes_full_cycle(self, pg_session: Session) -> None:
        """Ciclo APPROVE: RiskEngine aprueba el plan → ExecutionEngine persiste
        Order/Trade/Position en la DB del Orchestrator real.

        El tick previo siembra el VolatilityAssessment (ATR) que ExecutionEngine
        requiere para registrar el trailing stop. El RiskValidationResult se
        construye a mano porque GPT/Aggregator no están auto-wired en _tick()
        todavía (tarjeta [145]).
        """
        orch = Orchestrator(session=pg_session)
        try:
            mds = orch.cycle_runner._market_data_service
            assert mds is not None
            mds.tick_all()

            decision = _make_approve_decision()
            cfg = get_config()

            aggregation_id = _make_aggregation_id()
            risk_result = risk_engine.validate(
                aggregation=_make_aggregation_result(aggregation_id, decision),
                decision=decision,
                daily_loss_usdt=Decimal("0"),
                total_loss_usdt=Decimal("0"),
                config=cfg,
            )

            assert risk_result.decision == RiskDecision.APPROVE

            assert orch.execution_engine is not None
            result = orch.execution_engine.execute_approved_plan(decision, risk_result)

            assert result.position_registered is True
            assert result.trade_id is not None

            assert orch._bot_run is not None
            bot_run_id = orch._bot_run.id

            order_row = OrderRepository(pg_session).get_by_client_order_id(decision.decision_id)
            assert order_row is not None
            assert order_row.bot_run_id == bot_run_id
            assert order_row.status == OrderStatus.FILLED.value
            assert order_row.trade_id == result.trade_id

            trades = TradeRepository(pg_session).list_open(bot_run_id)
            assert len(trades) == 1
            assert trades[0].direction == "LONG"

            position = PositionRepository(pg_session).get_open_by_symbol(bot_run_id, _SYMBOL)
            assert position is not None
            assert position.stop_loss == Decimal(str(decision.stop_loss))
            assert position.take_profit == Decimal(str(decision.take_profit))

            assert orch.position_manager is not None
            pm_config = orch.position_manager.get_config(_SYMBOL)
            assert pm_config is not None
            assert pm_config.stop_loss == Decimal(str(decision.stop_loss))
            assert pm_config.take_profit == Decimal(str(decision.take_profit))
        finally:
            orch.close()

    def test_no_operar_outcome_does_not_execute(self, pg_session: Session) -> None:
        """Ciclo NO_OPERAR: decision.execute=False → RiskEngine emite NO_OPERAR →
        ExecutionEngine rechaza el plan (no es ejecutable).

        NO_OPERAR no es un rechazo del Risk Engine: el Aggregator determinó que
        no hay edge suficiente. El Risk Engine lo propaga sin evaluar reglas.
        """
        orch = Orchestrator(session=pg_session)
        try:
            decision = _make_no_operar_decision()
            cfg = get_config()

            aggregation_id = _make_aggregation_id()
            risk_result = risk_engine.validate(
                aggregation=_make_aggregation_result(aggregation_id, decision),
                decision=decision,
                daily_loss_usdt=Decimal("0"),
                total_loss_usdt=Decimal("0"),
                config=cfg,
            )

            assert risk_result.decision == RiskDecision.NO_OPERAR

            assert orch.execution_engine is not None
            with pytest.raises(ValueError, match="no es ejecutable"):
                orch.execution_engine.execute_approved_plan(decision, risk_result)
        finally:
            orch.close()

    def test_block_outcome_does_not_execute(self, pg_session: Session) -> None:
        """Ciclo BLOCK: daily drawdown excedido → RiskEngine emite BLOCK →
        ExecutionEngine rechaza el plan.

        BLOCK es un rechazo activo del Risk Engine (a diferencia de NO_OPERAR,
        que es propagación de la decisión del Aggregator). Se fuerza pasando
        daily_loss_usdt muy superior al límite configurado.
        """
        orch = Orchestrator(session=pg_session)
        try:
            decision = _make_block_decision()
            cfg = get_config()

            aggregation_id = _make_aggregation_id()
            risk_result = risk_engine.validate(
                aggregation=_make_aggregation_result(aggregation_id, decision),
                decision=decision,
                daily_loss_usdt=Decimal("10000"),  # garantiza daily_drawdown BLOCK
                total_loss_usdt=Decimal("0"),
                config=cfg,
            )

            assert risk_result.decision == RiskDecision.BLOCK

            assert orch.execution_engine is not None
            with pytest.raises(ValueError, match="no es ejecutable"):
                orch.execution_engine.execute_approved_plan(decision, risk_result)
        finally:
            orch.close()


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _make_aggregation_result(
    aggregation_id: str, decision: ModelDecision
) -> DecisionAggregationResult:
    """Construye un DecisionAggregationResult mínimo para pasarle al RiskEngine.

    El RiskEngine solo lee `aggregation_id`, `decision_id` y `symbol` del
    aggregation para construir el RiskValidationResult; el resto de los campos
    del aggregation no afectan la lógica de validate().
    """
    final_action = (
        DecisionType.NO_OPERAR if not decision.execute else decision.decision
    )

    return DecisionAggregationResult(
        aggregation_id=aggregation_id,
        decision_id=decision.decision_id,
        symbol=decision.symbol,
        timestamp_utc=_NOW,
        contributing_sources=ContributingSources(
            quant_score=0.5,
            gpt_context_score=0.5,
            regime_factor=0.5,
            volatility_factor=0.5,
        ),
        aggregated_score=0.5,
        final_action=final_action,
        contradictions_detected=[],
    )
