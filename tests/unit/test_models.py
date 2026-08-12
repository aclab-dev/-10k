"""Tests unitarios de los modelos SQLAlchemy del Anexo B.

Usan SQLite en memoria para verificar estructura, constraints y relaciones
sin necesidad de un servidor PostgreSQL. Los tipos JSONB se mapean a JSON
en SQLite automáticamente por SQLAlchemy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from backend.storage.database import Base
from backend.storage.models import (
    BacktestResult,
    BacktestRun,
    BotRun,
    BotState,
    Decision,
    DecisionAggregation,
    ErrorRecord,
    FeaturePackage,
    HistoricalReplayRun,
    HistoricalReplaySnapshot,
    KillSwitchEvent,
    MarketRegime,
    MarketSnapshot,
    ModelRequest,
    ModelResponse,
    NewsContext,
    Order,
    Position,
    PositionEvent,
    QuantSignal,
    RiskValidation,
    StrategyPerformance,
    StrategySetup,
    SystemEvent,
    TokenUsage,
    Trade,
    VolatilityAssessment,
)

ALL_EXPECTED_TABLES = {
    "bot_runs",
    "bot_state",
    "accounts_state",
    "market_snapshots",
    "quant_signals",
    "market_regimes",
    "volatility_assessments",
    "feature_packages",
    "model_requests",
    "model_responses",
    "decisions",
    "decision_aggregations",
    "risk_validations",
    "trades",
    "orders",
    "positions",
    "position_events",
    "strategy_performance",
    "historical_replay_runs",
    "historical_replay_snapshots",
    "backtest_runs",
    "backtest_results",
    "news_context",
    "token_usage",
    "system_events",
    "errors",
    "kill_switch_events",
    "strategy_setups",
    # Operativas del dashboard (F15), fuera del Anexo B
    "login_attempts",
    "login_lockouts",
}


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    # JSONB no existe en SQLite; el dialecto lo mapea a JSON silenciosamente
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _make_bot_run(session: Session) -> BotRun:
    run = BotRun(
        id=_uid(),
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"mode": "PAPER"},
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    return run


# ---------------------------------------------------------------------------
# Estructura
# ---------------------------------------------------------------------------


class TestTableCoverage:
    def test_all_expected_tables_registered(self, engine):
        inspector = inspect(engine)
        actual = set(inspector.get_table_names())
        assert actual == ALL_EXPECTED_TABLES, (
            f"Tablas faltantes: {ALL_EXPECTED_TABLES - actual}\n"
            f"Tablas extra: {actual - ALL_EXPECTED_TABLES}"
        )

    def test_base_metadata_has_no_unlisted_tables(self):
        assert len(Base.metadata.tables) == len(ALL_EXPECTED_TABLES)


# ---------------------------------------------------------------------------
# bot_runs
# ---------------------------------------------------------------------------


class TestBotRun:
    def test_create_and_read(self, session):
        run = _make_bot_run(session)
        fetched = session.get(BotRun, run.id)
        assert fetched is not None
        assert fetched.environment == "PAPER"
        assert fetched.status == "RUNNING"

    def test_started_at_defaults_to_now(self, session):
        run = _make_bot_run(session)
        assert run.started_at is not None
        assert isinstance(run.started_at, datetime)

    def test_ended_at_nullable(self, session):
        run = _make_bot_run(session)
        assert run.ended_at is None

    def test_config_snapshot_json(self, session):
        run = BotRun(
            id=_uid(),
            environment="TESTNET",
            app_version="0.1.0",
            config_snapshot={"leverage": 5, "symbols": ["BTCUSDT"]},
        )
        session.add(run)
        session.flush()
        fetched = session.get(BotRun, run.id)
        assert fetched.config_snapshot["leverage"] == 5


# ---------------------------------------------------------------------------
# bot_state
# ---------------------------------------------------------------------------


class TestBotState:
    def test_create_with_fk(self, session):
        run = _make_bot_run(session)
        state = BotState(
            id=_uid(),
            bot_run_id=run.id,
            state="ACTIVE",
            previous_state=None,
            created_at=_now(),
        )
        session.add(state)
        session.flush()
        assert state.bot_run_id == run.id

    def test_relationship_bot_run(self, session):
        run = _make_bot_run(session)
        state = BotState(id=_uid(), bot_run_id=run.id, state="SAFE_MODE", created_at=_now())
        session.add(state)
        session.flush()
        assert state.bot_run.id == run.id


# ---------------------------------------------------------------------------
# market_snapshots
# ---------------------------------------------------------------------------


class TestMarketSnapshot:
    def test_create(self, session):
        run = _make_bot_run(session)
        snap = MarketSnapshot(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            open=60000,
            high=61000,
            low=59000,
            close=60500,
            volume=1000,
        )
        session.add(snap)
        session.flush()
        fetched = session.get(MarketSnapshot, snap.id)
        assert fetched.symbol == "BTCUSDT"
        assert fetched.close == 60500

    def test_optional_fields_nullable(self, session):
        run = _make_bot_run(session)
        snap = MarketSnapshot(
            id=_uid(),
            bot_run_id=run.id,
            symbol="ETHUSDT",
            timestamp=_now(),
            open=3000,
            high=3100,
            low=2900,
            close=3050,
            volume=500,
        )
        session.add(snap)
        session.flush()
        assert snap.funding_rate is None
        assert snap.open_interest is None


# ---------------------------------------------------------------------------
# quant_signals
# ---------------------------------------------------------------------------


class TestQuantSignal:
    def test_create_with_scores(self, session):
        run = _make_bot_run(session)
        sig = QuantSignal(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            momentum_score=0.7,
            composite_score=0.65,
            raw_signals={"rsi": 58, "macd": 0.02},
        )
        session.add(sig)
        session.flush()
        fetched = session.get(QuantSignal, sig.id)
        assert fetched.composite_score == 0.65
        assert fetched.raw_signals["rsi"] == 58


# ---------------------------------------------------------------------------
# decisions — verifica la cadena de decisión
# ---------------------------------------------------------------------------


class TestDecisionChain:
    def _make_chain(self, session):
        run = _make_bot_run(session)
        snap = MarketSnapshot(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            open=60000,
            high=61000,
            low=59000,
            close=60500,
            volume=1000,
        )
        session.add(snap)
        fp = FeaturePackage(
            id=_uid(),
            bot_run_id=run.id,
            market_snapshot_id=snap.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            version="v1",
            features={"x": 1},
            features_hash=_uid(),
        )
        session.add(fp)
        req = ModelRequest(
            id=_uid(),
            bot_run_id=run.id,
            feature_package_id=fp.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            model="gpt-5.5",
            context={"prompt": "..."},
            request_hash="def456",
        )
        session.add(req)
        resp = ModelResponse(
            id=_uid(),
            model_request_id=req.id,
            timestamp=_now(),
            raw_response='{"action":"OPEN"}',
            model="gpt-5.5",
            is_valid_schema=True,
        )
        session.add(resp)
        dec = Decision(
            id=_uid(),
            bot_run_id=run.id,
            model_response_id=resp.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            action="OPEN",
            direction="LONG",
            confidence=0.82,
            margin_usdt=10,
            leverage=3,
        )
        session.add(dec)
        session.flush()
        return run, dec

    def test_decision_created(self, session):
        _, dec = self._make_chain(session)
        fetched = session.get(Decision, dec.id)
        assert fetched.action == "OPEN"
        assert fetched.confidence == 0.82

    def test_decision_aggregation(self, session):
        run, dec = self._make_chain(session)
        agg = DecisionAggregation(
            id=_uid(),
            bot_run_id=run.id,
            decision_id=dec.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            quant_score=0.75,
            gpt_confidence=0.82,
            aggregated_score=0.78,
            final_action="OPEN",
        )
        session.add(agg)
        session.flush()
        assert agg.final_action == "OPEN"

    def test_risk_validation_results(self, session):
        run, dec = self._make_chain(session)
        agg = DecisionAggregation(
            id=_uid(),
            bot_run_id=run.id,
            decision_id=dec.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            final_action="OPEN",
        )
        session.add(agg)
        session.flush()
        for result in ("APPROVE", "ADJUST_DOWN", "BLOCK"):
            rv = RiskValidation(
                id=_uid(),
                bot_run_id=run.id,
                decision_aggregation_id=agg.id,
                symbol="BTCUSDT",
                timestamp=_now(),
                result=result,
            )
            session.add(rv)
        session.flush()


# ---------------------------------------------------------------------------
# trades + orders + positions + position_events
# ---------------------------------------------------------------------------


class TestTradeLifecycle:
    def test_full_trade_lifecycle(self, session):
        run = _make_bot_run(session)

        trade = Trade(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            environment="PAPER",
            direction="LONG",
            margin_usdt=10,
            leverage=3,
            opened_at=_now(),
        )
        session.add(trade)
        session.flush()

        order = Order(
            id=_uid(),
            bot_run_id=run.id,
            trade_id=trade.id,
            client_order_id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            environment="PAPER",
            order_type="MARKET",
            side="BUY",
            quantity="0.001",
            is_simulated=True,
            created_at=_now(),
        )
        session.add(order)

        pos = Position(
            id=_uid(),
            bot_run_id=run.id,
            trade_id=trade.id,
            symbol="BTCUSDT",
            environment="PAPER",
            direction="LONG",
            quantity="0.001",
            entry_price=60000,
            margin_usdt=10,
            leverage=3,
            opened_at=_now(),
            updated_at=_now(),
        )
        session.add(pos)
        session.flush()

        event = PositionEvent(
            id=_uid(),
            position_id=pos.id,
            event_type="SL_UPDATE",
            old_value=58000,
            new_value=59000,
            created_at=_now(),
        )
        session.add(event)
        session.flush()

        assert order.trade_id == trade.id
        assert pos.trade_id == trade.id
        assert event.position_id == pos.id

    def test_is_simulated_default_true(self, session):
        run = _make_bot_run(session)
        trade = Trade(
            id=_uid(),
            bot_run_id=run.id,
            symbol="ETHUSDT",
            environment="PAPER",
            direction="SHORT",
            margin_usdt=5,
            leverage=2,
            opened_at=_now(),
        )
        session.add(trade)
        session.flush()
        order = Order(
            id=_uid(),
            bot_run_id=run.id,
            trade_id=trade.id,
            client_order_id=str(uuid.uuid4()),
            symbol="ETHUSDT",
            environment="PAPER",
            order_type="MARKET",
            side="SELL",
            quantity="0.01",
            created_at=_now(),
        )
        session.add(order)
        session.flush()
        assert order.is_simulated is True


# ---------------------------------------------------------------------------
# backtest + replay
# ---------------------------------------------------------------------------


class TestBacktestAndReplay:
    def test_backtest_run_and_result(self, session):
        run = _make_bot_run(session)
        bt = BacktestRun(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            started_at=_now(),
            period_start=_now(),
            period_end=_now(),
            config_snapshot={"fee": 0.0004},
        )
        session.add(bt)
        session.flush()
        result = BacktestResult(
            id=_uid(),
            backtest_run_id=bt.id,
            total_trades=100,
            winning_trades=55,
            losing_trades=45,
            win_rate=0.55,
            total_pnl=12,
        )
        session.add(result)
        session.flush()
        assert result.backtest_run_id == bt.id

    def test_historical_replay_run_and_snapshot(self, session):
        run = _make_bot_run(session)
        rr = HistoricalReplayRun(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            started_at=_now(),
            period_start=_now(),
            period_end=_now(),
            config_snapshot={},
        )
        session.add(rr)
        session.flush()
        snap = HistoricalReplaySnapshot(
            id=_uid(),
            replay_run_id=rr.id,
            sequence_num=1,
            market_snapshot={"close": 60000},
        )
        session.add(snap)
        session.flush()
        assert snap.replay_run_id == rr.id


# ---------------------------------------------------------------------------
# audit tables
# ---------------------------------------------------------------------------


class TestAuditTables:
    def test_system_event(self, session):
        run = _make_bot_run(session)
        ev = SystemEvent(
            id=_uid(),
            bot_run_id=run.id,
            timestamp=_now(),
            event_type="CYCLE_START",
            severity="INFO",
            message="Ciclo iniciado",
        )
        session.add(ev)
        session.flush()
        assert ev.severity == "INFO"

    def test_error_record(self, session):
        run = _make_bot_run(session)
        err = ErrorRecord(
            id=_uid(),
            bot_run_id=run.id,
            timestamp=_now(),
            module="risk_engine",
            error_type="ValueError",
            message="leverage fuera de rango",
            recovered=False,
        )
        session.add(err)
        session.flush()
        assert err.recovered is False

    def test_kill_switch_event(self, session):
        run = _make_bot_run(session)
        ks = KillSwitchEvent(
            id=_uid(),
            bot_run_id=run.id,
            timestamp=_now(),
            trigger_reason="Drawdown > 10%",
            state_before="ACTIVE",
            action_taken="HALT_AND_CLOSE_ALL",
            positions_closed=1,
            orders_cancelled=2,
        )
        session.add(ks)
        session.flush()
        assert ks.requires_manual_review is True

    def test_token_usage(self, session):
        run = _make_bot_run(session)
        tu = TokenUsage(
            id=_uid(),
            bot_run_id=run.id,
            timestamp=_now(),
            model="gpt-5.5",
            prompt_tokens=1200,
            completion_tokens=150,
            total_tokens=1350,
        )
        session.add(tu)
        session.flush()
        assert tu.total_tokens == 1350

    def test_news_context(self, session):
        run = _make_bot_run(session)
        nc = NewsContext(
            id=_uid(),
            bot_run_id=run.id,
            timestamp=_now(),
            symbol="BTCUSDT",
            source="CoinDesk",
            headline="BTC hits 100k",
            sentiment="POSITIVE",
            relevance_score=0.9,
        )
        session.add(nc)
        session.flush()
        assert nc.sentiment == "POSITIVE"


# ---------------------------------------------------------------------------
# market_regimes, volatility_assessments, strategy_performance
# ---------------------------------------------------------------------------


class TestMarketRegime:
    def test_create(self, session):
        run = _make_bot_run(session)
        regime = MarketRegime(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            regime="TRENDING",
            confidence=0.85,
        )
        session.add(regime)
        session.flush()
        fetched = session.get(MarketRegime, regime.id)
        assert fetched.regime == "TRENDING"
        assert fetched.confidence == 0.85

    def test_optional_snapshot_fk(self, session):
        run = _make_bot_run(session)
        snap = MarketSnapshot(
            id=_uid(),
            bot_run_id=run.id,
            symbol="ETHUSDT",
            timestamp=_now(),
            open=3000,
            high=3100,
            low=2900,
            close=3050,
            volume=500,
        )
        session.add(snap)
        session.flush()
        regime = MarketRegime(
            id=_uid(),
            bot_run_id=run.id,
            market_snapshot_id=snap.id,
            symbol="ETHUSDT",
            timestamp=_now(),
            regime="RANGING",
            confidence=0.6,
        )
        session.add(regime)
        session.flush()
        assert regime.market_snapshot_id == snap.id


class TestVolatilityAssessment:
    def test_create(self, session):
        run = _make_bot_run(session)
        va = VolatilityAssessment(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            atr_percent=2.5,
            volatility_score=0.7,
            leverage_cap=5,
            liquidation_risk_score=0.3,
        )
        session.add(va)
        session.flush()
        fetched = session.get(VolatilityAssessment, va.id)
        assert fetched.leverage_cap == 5
        assert fetched.volatility_score == 0.7

    def test_all_nullable_fields(self, session):
        run = _make_bot_run(session)
        va = VolatilityAssessment(
            id=_uid(),
            bot_run_id=run.id,
            symbol="SOLUSDT",
            timestamp=_now(),
        )
        session.add(va)
        session.flush()
        assert va.atr is None
        assert va.atr_percent is None


class TestStrategyPerformance:
    def test_create_and_aggregate(self, session):
        run = _make_bot_run(session)
        sp = StrategyPerformance(
            id=_uid(),
            bot_run_id=run.id,
            symbol="BTCUSDT",
            regime="TRENDING",
            setup_type="BREAKOUT",
            total_trades=50,
            winning_trades=30,
            losing_trades=20,
            win_rate=0.6,
            sharpe_ratio=1.4,
            max_drawdown=0.08,
        )
        session.add(sp)
        session.flush()
        fetched = session.get(StrategyPerformance, sp.id)
        assert fetched.win_rate == 0.6
        assert fetched.total_trades == 50

    def test_optional_period(self, session):
        run = _make_bot_run(session)
        sp = StrategyPerformance(
            id=_uid(),
            bot_run_id=run.id,
            symbol="ETHUSDT",
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
        )
        session.add(sp)
        session.flush()
        assert sp.period_start is None
        assert sp.period_end is None


# ---------------------------------------------------------------------------
# 28. strategy_setups  (F12)
# ---------------------------------------------------------------------------


class TestStrategySetup:
    def test_create_and_read(self, session):
        setup = StrategySetup(
            id=_uid(),
            name="breakout_pullback_v1",
            slug="breakout_pullback",
            version="v1",
            parameters={},
            metadata_={},
            is_active=True,
        )
        session.add(setup)
        session.flush()
        fetched = session.get(StrategySetup, setup.id)
        assert fetched is not None
        assert fetched.name == "breakout_pullback_v1"
        assert fetched.slug == "breakout_pullback"
        assert fetched.version == "v1"
        assert fetched.is_active is True

    def test_parameters_and_metadata_stored_as_json(self, session):
        setup = StrategySetup(
            id=_uid(),
            name="trend_continuation_v1",
            slug="trend_continuation",
            version="v1",
            parameters={"min_confidence_override": 0.80, "preferred_regimes": ["TRENDING_UP"]},
            metadata_={"description": "Trend continuation setup", "author": "quant_team"},
            is_active=True,
        )
        session.add(setup)
        session.flush()
        fetched = session.get(StrategySetup, setup.id)
        assert fetched.parameters["min_confidence_override"] == 0.80
        assert fetched.metadata_["author"] == "quant_team"
