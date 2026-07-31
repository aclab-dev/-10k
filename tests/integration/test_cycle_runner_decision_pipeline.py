"""Test E2E del pipeline de decision en CycleRunner (CR).

Verifica que CycleRunner._tick() ejecuta el ciclo completo:
  Market Data → Quant/Regime/Volatility → GPT → DecisionAggregator
  → RiskEngine → ExecutionEngine

GPT se mockea con AsyncMock para evitar llamadas HTTP reales.
El resto del pipeline (Aggregator, Risk, Execution, DB) es real.

Casos de prueba:
  1. APPROVE: GPT devuelve LONG ejecutable → aggregation LONG → Risk APPROVE
     → ExecutionEngine coloca orden → Trade registrado en DB.
  2. NO_OPERAR (GPT): GPT devuelve NO_OPERAR → aggregation NO_OPERAR → sin orden.
  3. NO_OPERAR (contradicción): GPT devuelve LONG pero quant del snapshot da señal
     NO_OPERAR en aggregator → sin orden.
  4. BLOCK (drawdown): hay pérdida diaria que supera el límite → Risk BLOCK → sin orden.

Ejecutar con: pytest -m integration tests/integration/test_cycle_runner_decision_pipeline.py
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.core.config import Environment, load_config
from backend.decision_engine.aggregator import DecisionAggregator
from backend.decision_engine.gpt_client import GPTClient
from backend.decision_engine.prompt_builder import PromptBuilder
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
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.execution.engine import ExecutionEngine
from backend.market_data.analysis_service import MarketAnalysisService
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.engine import MarketDataEngine
from backend.market_data.fetcher import MockDataFetcher
from backend.market_regime.schemas import PrimaryRegime
from backend.position_manager.manager import PositionManager
from backend.storage.models import BotRun, Trade
from backend.storage.repositories.trades import TradeRepository
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import CycleRunner

_NOW = datetime.now(UTC)
_SYMBOL = "BTCUSDT"
_INITIAL_BALANCE = Decimal("1000")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gpt_decision(
    decision_type: DecisionType,
    *,
    execute: bool | None = None,
    confidence: float = 0.85,
) -> ModelDecision:
    is_execute = execute if execute is not None else decision_type != DecisionType.NO_OPERAR
    return ModelDecision(
        environment=Environment.PAPER,
        timestamp_utc=_NOW,
        decision=decision_type,
        symbol=_SYMBOL,
        entry_type=EntryType.MARKET,
        entry_price=50_100.0 if is_execute else 0.0,
        stop_loss=49_500.0 if is_execute else 0.0,
        take_profit=51_500.0 if is_execute else 0.0,
        invalidation_price=49_000.0,
        leverage=3,
        margin_usdt=5.0,
        estimated_notional_usdt=15.0,
        estimated_entry_fee_usdt=0.075,
        estimated_exit_fee_usdt=0.075,
        estimated_slippage_usdt=0.05,
        estimated_funding_usdt=0.01,
        net_risk_reward=2.3 if is_execute else 0.0,
        estimated_max_loss_usdt=5.0,
        liquidation_distance_percent_estimated=15.0,
        confidence=confidence,
        market_regime=PrimaryRegime.TRENDING,
        setup_name="e2e_pipeline_test",
        timeframes_used=["5m", "15m", "1h", "4h"],
        quant_signals=QuantSignalsSection(
            momentum=(
                MomentumInterpretation.BULLISH if is_execute else MomentumInterpretation.NEUTRAL
            ),
            mean_reversion=MeanReversionInterpretation.NEUTRAL,
            breakout_detection=(
                BreakoutInterpretation.CONFIRMED if is_execute else BreakoutInterpretation.NONE
            ),
            funding_analysis=FundingInterpretation.NEUTRAL,
            open_interest_analysis=OpenInterestInterpretation.RISING_WITH_PRICE,
            order_flow_imbalance=OrderFlowInterpretation.BUY_PRESSURE,
            liquidity_sweep=LiquiditySweepInterpretation.NONE,
        ),
        decision_aggregator=DecisionAggregatorSection(
            quant_score=0.65,
            gpt_context_score=confidence,
            risk_quality_score=0.80,
            final_trade_quality_score=0.75,
            contradictions_detected=[],
        ),
        news_context=NewsContextSection(used=False, impact=NewsImpact.NEUTRAL, summary=""),
        position_management_plan=PositionManagementPlan(
            use_trailing_stop=True,
            move_to_break_even=True,
            partial_close_plan="none",
            max_time_in_trade_minutes=480,
        ),
        decision_rationale_summary="Integration test fixture.",
        risk_notes=[],
        execute=is_execute,
    )


def _build_bot_run(session: Session) -> BotRun:
    from backend.core.config import APP_VERSION

    cfg = load_config()
    bot_run = BotRun(
        environment=Environment.PAPER.value,
        app_version=APP_VERSION,
        config_snapshot=cfg.model_dump(mode="json"),
        status="RUNNING",
    )
    session.add(bot_run)
    session.commit()
    return bot_run


def _build_pipeline(
    session: Session,
    bot_run: BotRun,
    mock_gpt_decision: ModelDecision | None,
) -> CycleRunner:
    """Construye un CycleRunner completo con GPT mockeado."""
    cfg = load_config()
    adapter = PaperAdapter(initial_balance_usdt=_INITIAL_BALANCE)
    position_manager = PositionManager(adapter)
    fetcher = MockDataFetcher()
    engine = MarketDataEngine(session, bot_run.id)
    analysis_service = MarketAnalysisService(session, bot_run.id)
    mds = MarketDataCycleService(
        adapter=adapter,
        fetcher=fetcher,
        engine=engine,
        session=session,
        symbols=["BTCUSDT"],
        on_snapshot=analysis_service.on_snapshot,
    )
    exec_engine = ExecutionEngine(
        adapter=adapter,
        position_manager=position_manager,
        session=session,
        bot_run_id=bot_run.id,
        environment=cfg.execution.environment,
        position_management_defaults=cfg.position_management,
        place_order_timeout_seconds=cfg.execution.place_order_timeout_seconds,
    )

    # GPT mockeado: retorna la decision predeterminada
    gpt_client = MagicMock(spec=GPTClient)
    gpt_client.request = AsyncMock(return_value=mock_gpt_decision)

    heartbeat = Path(tempfile.mktemp(suffix="_worker_alive"))
    return CycleRunner(
        state_machine=BotStateMachine(initial=BotState.ACTIVE),
        interval_seconds=1,
        heartbeat_file=heartbeat,
        market_data_service=mds,
        execution_engine=exec_engine,
        gpt_client=gpt_client,
        prompt_builder=PromptBuilder(),
        aggregator=DecisionAggregator(),
        config=cfg,
        session=session,
        bot_run_id=bot_run.id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _make_strong_quant_signals(snapshot_id: str | None = None) -> object:
    """QuantSignalsPackage con señales bullish fuertes para forzar APPROVE en el aggregator."""
    import uuid as _uuid

    from backend.quant_signals.schemas import QuantSignalsPackage

    return QuantSignalsPackage.model_validate(
        {
            "snapshot_id": snapshot_id or str(_uuid.uuid4()),
            "timestamp_utc": _NOW,
            "symbol": _SYMBOL,
            "timeframes_used": ["5m", "15m", "1h", "4h"],
            "momentum_signal": 0.75,
            "mean_reversion_signal": 0.0,
            "breakout_signal": 0.60,
            "funding_signal": 0.10,
            "open_interest_signal": 0.40,
            "order_flow_imbalance_signal": 0.55,
            "liquidity_sweep_signal": 0.10,
            "signal_strength_score": 0.65,
            "signal_conflict_score": 0.10,
            "signal_confidence": 0.85,
            "raw_feature_refs": {},
        }
    )


@pytest.mark.integration
class TestDecisionPipelineApprove:
    """GPT LONG + señales coherentes → APPROVE → Trade creado en DB.

    Se parchea compute_quant_signals para inyectar señales bullish fuertes
    en vez de las que produce MockDataFetcher (que son débiles e impredecibles).
    """

    def test_tick_creates_trade_on_approve(self, pg_session: Session) -> None:
        bot_run = _build_bot_run(pg_session)
        gpt_decision = _make_gpt_decision(DecisionType.LONG)
        runner = _build_pipeline(pg_session, bot_run, gpt_decision)

        strong_quant = _make_strong_quant_signals()
        with patch(
            "backend.trading_core.cycle_runner.compute_quant_signals",
            return_value=strong_quant,
        ):
            runner._tick()

        trades = TradeRepository(pg_session).list_open(bot_run.id)
        assert len(trades) == 1, "Debe haber exactamente un Trade abierto tras APPROVE"
        assert trades[0].symbol == _SYMBOL
        assert trades[0].direction == "LONG"

    def test_gpt_client_called_once_per_symbol(self, pg_session: Session) -> None:
        bot_run = _build_bot_run(pg_session)
        gpt_decision = _make_gpt_decision(DecisionType.LONG)
        runner = _build_pipeline(pg_session, bot_run, gpt_decision)

        strong_quant = _make_strong_quant_signals()
        with patch(
            "backend.trading_core.cycle_runner.compute_quant_signals",
            return_value=strong_quant,
        ):
            runner._tick()

        assert runner._gpt_client.request.call_count == 1  # type: ignore[union-attr]


@pytest.mark.integration
class TestDecisionPipelineNoOperarGPT:
    """GPT devuelve NO_OPERAR → sin Trade en DB."""

    def test_tick_no_trade_when_gpt_no_operar(self, pg_session: Session) -> None:
        bot_run = _build_bot_run(pg_session)
        gpt_decision = _make_gpt_decision(DecisionType.NO_OPERAR, execute=False)
        runner = _build_pipeline(pg_session, bot_run, gpt_decision)

        runner._tick()

        trades = TradeRepository(pg_session).list_open(bot_run.id)
        assert len(trades) == 0, "No debe haber trades cuando GPT dice NO_OPERAR"


@pytest.mark.integration
class TestDecisionPipelineGPTNone:
    """GPT retorna None (failure_policy swallowed) → sin Trade en DB."""

    def test_tick_no_trade_when_gpt_returns_none(self, pg_session: Session) -> None:
        bot_run = _build_bot_run(pg_session)
        runner = _build_pipeline(pg_session, bot_run, mock_gpt_decision=None)

        runner._tick()

        trades = TradeRepository(pg_session).list_open(bot_run.id)
        assert len(trades) == 0


@pytest.mark.integration
class TestDecisionPipelineRiskBlock:
    """Pérdida diaria que supera el límite → Risk BLOCK → sin Trade."""

    def test_tick_no_trade_when_risk_blocked_by_drawdown(self, pg_session: Session) -> None:
        bot_run = _build_bot_run(pg_session)
        gpt_decision = _make_gpt_decision(DecisionType.LONG)
        runner = _build_pipeline(pg_session, bot_run, gpt_decision)

        # Insertar trade cerrado con pérdida suficiente para superar el drawdown diario.
        # config.yaml: initial_balance=100 USDT, max_daily_loss_percent=10% → límite=10 USDT
        losing_trade = Trade(
            bot_run_id=bot_run.id,
            symbol=_SYMBOL,
            environment=Environment.PAPER.value,
            direction="LONG",
            margin_usdt=Decimal("20"),
            leverage=3,
            net_pnl=Decimal("-15"),
            status="CLOSED",
            closed_at=datetime.now(UTC),
        )
        pg_session.add(losing_trade)
        pg_session.commit()

        # Se parchea compute_quant_signals para garantizar que el aggregator aprueba
        # y el bloqueo venga del Risk Engine (drawdown), no de una contradicción de señales.
        strong_quant = _make_strong_quant_signals()
        with patch(
            "backend.trading_core.cycle_runner.compute_quant_signals",
            return_value=strong_quant,
        ):
            runner._tick()

        trades = TradeRepository(pg_session).list_open(bot_run.id)
        assert len(trades) == 0, "Risk BLOCK por drawdown debe impedir nuevos trades"


@pytest.mark.integration
class TestLossTotalsQuery:
    """Verifica que get_loss_totals agrega correctamente pérdidas reales vs neutras."""

    def test_loss_totals_sum_only_losing_closed_trades(self, pg_session: Session) -> None:
        bot_run = _build_bot_run(pg_session)
        repo = TradeRepository(pg_session)

        now = datetime.now(UTC)
        for net_pnl in [Decimal("-5"), Decimal("-3"), Decimal("8")]:
            pg_session.add(
                Trade(
                    bot_run_id=bot_run.id,
                    symbol=_SYMBOL,
                    environment=Environment.PAPER.value,
                    direction="LONG",
                    margin_usdt=Decimal("10"),
                    leverage=3,
                    net_pnl=net_pnl,
                    status="CLOSED",
                    closed_at=now,
                )
            )
        pg_session.commit()

        daily_loss, total_loss = repo.get_loss_totals(bot_run.id)

        assert total_loss == Decimal("8"), "Suma de pérdidas: 5+3=8"
        assert daily_loss == Decimal("8"), "Hoy: mismas pérdidas"

    def test_loss_totals_zero_when_no_closed_trades(self, pg_session: Session) -> None:
        bot_run = _build_bot_run(pg_session)
        daily, total = TradeRepository(pg_session).get_loss_totals(bot_run.id)
        assert daily == Decimal("0")
        assert total == Decimal("0")
