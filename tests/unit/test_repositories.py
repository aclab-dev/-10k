"""Tests unitarios de repositorios.

Usan SQLite en memoria (misma estrategia que test_models.py) para verificar
la lógica de consulta sin necesidad de un servidor PostgreSQL real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.core.config import Environment
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
)
from backend.market_data.schemas import MarketSnapshot as MarketSnapshotSchema
from backend.storage.database import Base
from backend.storage.models import (
    AccountState,
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
    Order,
    Position,
    PositionEvent,
    QuantSignal,
    RiskValidation,
    SystemEvent,
    TokenUsage,
    Trade,
    VolatilityAssessment,
)
from backend.storage.repositories import (
    AccountStateRepository,
    BacktestResultRepository,
    BacktestRunRepository,
    BotRunRepository,
    BotStateRepository,
    DecisionAggregationRepository,
    DecisionRepository,
    ErrorRecordRepository,
    FeaturePackageRepository,
    HistoricalReplayRunRepository,
    HistoricalReplaySnapshotRepository,
    KillSwitchEventRepository,
    MarketRegimeRepository,
    MarketSnapshotRepository,
    ModelRequestRepository,
    ModelResponseRepository,
    OrderRepository,
    PositionEventRepository,
    PositionRepository,
    QuantSignalRepository,
    RiskValidationRepository,
    SystemEventRepository,
    TokenUsageRepository,
    TradeRepository,
    VolatilityAssessmentRepository,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _bot_run(
    session: Session, status: str = "RUNNING", started_at: datetime | None = None
) -> BotRun:
    run = BotRun(
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"test": True},
        status=status,
    )
    if started_at is not None:
        run.started_at = started_at
    session.add(run)
    session.flush()
    return run


# ---------------------------------------------------------------------------
# BotRunRepository
# ---------------------------------------------------------------------------


class TestBotRunRepository:
    def test_save_and_get(self, session: Session) -> None:
        repo = BotRunRepository(session)
        run = _bot_run(session)
        fetched = repo.get_by_id(run.id)
        assert fetched is not None
        assert fetched.environment == "PAPER"

    def test_get_active_returns_running(self, session: Session) -> None:
        repo = BotRunRepository(session)
        run = _bot_run(session, status="RUNNING")
        active = repo.get_active()
        assert active is not None
        assert active.id == run.id

    def test_second_running_bot_run_violates_single_running_constraint(
        self, session: Session
    ) -> None:
        """F16 [114]: uq_bot_runs_single_running impide un segundo RUNNING en la DB.

        Antes de esta constraint, get_active() dependia de un order_by por
        started_at para desempatar entre dos RUNNING conviviendo (ver docstring
        del metodo) — ese escenario ahora es imposible: el segundo insert falla
        en la DB, no llega a convivir para que get_active() tenga que elegir.
        """
        _bot_run(session, status="RUNNING", started_at=_now() - timedelta(hours=2))
        with pytest.raises(IntegrityError):
            _bot_run(session, status="RUNNING", started_at=_now())
        session.rollback()

    def test_get_active_ignores_non_running(self, session: Session) -> None:
        repo = BotRunRepository(session)
        _bot_run(session, status="STOPPED", started_at=_now())
        _bot_run(session, status="CRASHED", started_at=_now())
        assert repo.get_active() is None

    def test_close_orphan_running_marks_crashed(self, session: Session) -> None:
        repo = BotRunRepository(session)
        orphan = _bot_run(session, status="RUNNING", started_at=_now() - timedelta(hours=1))
        closed = repo.close_orphan_running(reason="murio feo")
        assert [run.id for run in closed] == [orphan.id]
        assert orphan.status == "CRASHED"
        assert orphan.ended_at is not None
        assert orphan.notes == "murio feo"
        assert repo.get_active() is None

    def test_close_orphan_running_leaves_closed_runs_alone(self, session: Session) -> None:
        repo = BotRunRepository(session)
        stopped = _bot_run(session, status="STOPPED", started_at=_now() - timedelta(hours=3))
        stopped_ended_at = stopped.ended_at
        repo.close_orphan_running(reason="murio feo")
        assert stopped.status == "STOPPED"
        assert stopped.ended_at == stopped_ended_at
        assert stopped.notes is None

    def test_close_orphan_running_appends_to_existing_notes(self, session: Session) -> None:
        repo = BotRunRepository(session)
        orphan = _bot_run(session, status="RUNNING")
        orphan.notes = "nota previa"
        session.flush()
        repo.close_orphan_running(reason="murio feo")
        assert orphan.notes == "nota previa\nmurio feo"

    def test_close_orphan_running_without_orphans(self, session: Session) -> None:
        repo = BotRunRepository(session)
        assert repo.close_orphan_running(reason="murio feo") == []

    def test_close_sets_status(self, session: Session) -> None:
        repo = BotRunRepository(session)
        run = _bot_run(session)
        repo.close(run, status="STOPPED")
        assert run.status == "STOPPED"
        assert run.ended_at is not None

    def test_get_by_id_missing(self, session: Session) -> None:
        repo = BotRunRepository(session)
        assert repo.get_by_id(_uid()) is None


# ---------------------------------------------------------------------------
# BotStateRepository
# ---------------------------------------------------------------------------


class TestBotStateRepository:
    def test_get_latest(self, session: Session) -> None:
        run = _bot_run(session)
        s1 = BotState(bot_run_id=run.id, state="ACTIVE")
        s2 = BotState(bot_run_id=run.id, state="SAFE_MODE")
        session.add_all([s1, s2])
        session.flush()

        repo = BotStateRepository(session)
        latest = repo.get_latest(run.id)
        assert latest is not None
        assert latest.state == "SAFE_MODE"

    def test_list_by_bot_run(self, session: Session) -> None:
        run = _bot_run(session)
        for state in ("ACTIVE", "HALTED"):
            session.add(BotState(bot_run_id=run.id, state=state))
        session.flush()

        repo = BotStateRepository(session)
        results = repo.list_by_bot_run(run.id)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# AccountStateRepository
# ---------------------------------------------------------------------------


class TestAccountStateRepository:
    def test_get_latest(self, session: Session) -> None:
        run = _bot_run(session)
        t1 = datetime(2026, 1, 1, tzinfo=UTC)
        t2 = datetime(2026, 1, 2, tzinfo=UTC)
        session.add(
            AccountState(
                bot_run_id=run.id,
                timestamp=t1,
                balance_usdt=Decimal("90"),
                equity_usdt=Decimal("90"),
                environment="PAPER",
            )
        )
        session.add(
            AccountState(
                bot_run_id=run.id,
                timestamp=t2,
                balance_usdt=Decimal("100"),
                equity_usdt=Decimal("100"),
                environment="PAPER",
            )
        )
        session.flush()

        repo = AccountStateRepository(session)
        latest = repo.get_latest(run.id)
        assert latest is not None
        assert latest.balance_usdt == Decimal("100")


# ---------------------------------------------------------------------------
# MarketSnapshotRepository
# ---------------------------------------------------------------------------


class TestMarketSnapshotRepository:
    def _snap(self, session: Session, run: BotRun, symbol: str, ts: datetime) -> MarketSnapshot:
        snap = MarketSnapshot(
            bot_run_id=run.id,
            symbol=symbol,
            timestamp=ts,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=Decimal("1000"),
        )
        session.add(snap)
        session.flush()
        return snap

    def test_get_latest_by_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        self._snap(session, run, "BTCUSDT", datetime(2026, 1, 1, tzinfo=UTC))
        self._snap(session, run, "BTCUSDT", datetime(2026, 1, 2, tzinfo=UTC))

        repo = MarketSnapshotRepository(session)
        latest = repo.get_latest_by_symbol(run.id, "BTCUSDT")
        assert latest is not None
        # SQLite strips timezone info; compare naive date part only
        assert latest.timestamp.replace(tzinfo=None) == datetime(2026, 1, 2)

    def test_get_latest_ignores_other_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        self._snap(session, run, "ETHUSDT", datetime(2026, 1, 1, tzinfo=UTC))

        repo = MarketSnapshotRepository(session)
        assert repo.get_latest_by_symbol(run.id, "BTCUSDT") is None

    def test_list_by_symbol_since_filters(self, session: Session) -> None:
        run = _bot_run(session)
        t1 = datetime(2026, 1, 1, tzinfo=UTC)
        t2 = datetime(2026, 1, 2, tzinfo=UTC)
        t3 = datetime(2026, 1, 3, tzinfo=UTC)
        for ts in (t1, t2, t3):
            self._snap(session, run, "BTCUSDT", ts)

        repo = MarketSnapshotRepository(session)
        results = repo.list_by_symbol(run.id, "BTCUSDT", since=t2)
        assert len(results) == 2

    def test_save_snapshot_persists_fields_from_schema(self, session: Session) -> None:
        run = _bot_run(session)
        now = datetime(2026, 1, 5, tzinfo=UTC)
        candle = CandleData(
            open=Decimal("50000"),
            high=Decimal("50100"),
            low=Decimal("49900"),
            close=Decimal("50010"),
            volume=Decimal("100"),
            n_candles=10,
        )
        schema_snap = MarketSnapshotSchema(
            timestamp_utc=now,
            exchange=Exchange.BINGX,
            environment=Environment.PAPER,
            symbol="SOLUSDT",
            last_price=Decimal("50005"),
            bid=Decimal("50000"),
            ask=Decimal("50010"),
            spread_absolute=Decimal("10"),
            spread_percent=Decimal("0.02"),
            candles=Candles(tf_5m=candle, tf_15m=candle, tf_1h=candle, tf_4h=candle),
            volume=Decimal("1000"),
            account_balance_usdt=Decimal("500"),
            open_positions_count=0,
            active_orders_count=0,
            latency_ms=50,
            exchange_server_time=now,
            local_time=now,
            clock_skew_ms=0,
            data_freshness_status=DataFreshnessStatus.FRESH,
            coherence_status=CoherenceStatus.OK,
        )

        repo = MarketSnapshotRepository(session)
        orm = repo.save_snapshot(schema_snap, run.id)

        assert orm.id == schema_snap.snapshot_id
        assert orm.bot_run_id == run.id
        assert orm.symbol == "SOLUSDT"
        # to_db_kwargs maps last_price → close (not the candle's close field)
        assert orm.close == Decimal("50005")
        assert orm.bid == Decimal("50000")
        assert orm.ask == Decimal("50010")
        # spread maps to spread_absolute, not spread_percent
        assert orm.spread == Decimal("10")
        assert orm.open == candle.open
        assert orm.high == candle.high
        assert orm.low == candle.low
        assert orm.volume == Decimal("1000")


# ---------------------------------------------------------------------------
# QuantSignalRepository
# ---------------------------------------------------------------------------


class TestQuantSignalRepository:
    def test_get_latest_by_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        for ts in (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)):
            session.add(
                QuantSignal(
                    bot_run_id=run.id,
                    symbol="BTCUSDT",
                    timestamp=ts,
                )
            )
        session.flush()

        repo = QuantSignalRepository(session)
        latest = repo.get_latest_by_symbol(run.id, "BTCUSDT")
        assert latest is not None
        assert latest.timestamp.replace(tzinfo=None) == datetime(2026, 1, 2)


# ---------------------------------------------------------------------------
# MarketRegimeRepository
# ---------------------------------------------------------------------------


class TestMarketRegimeRepository:
    def test_get_latest_by_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        session.add(
            MarketRegime(
                bot_run_id=run.id,
                symbol="BTCUSDT",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                regime="TRENDING",
                confidence=0.9,
            )
        )
        session.flush()

        repo = MarketRegimeRepository(session)
        latest = repo.get_latest_by_symbol(run.id, "BTCUSDT")
        assert latest is not None
        assert latest.regime == "TRENDING"


# ---------------------------------------------------------------------------
# VolatilityAssessmentRepository
# ---------------------------------------------------------------------------


class TestVolatilityAssessmentRepository:
    def test_get_latest_by_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        session.add(
            VolatilityAssessment(
                bot_run_id=run.id,
                symbol="ETHUSDT",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                leverage_cap=5,
            )
        )
        session.flush()

        repo = VolatilityAssessmentRepository(session)
        latest = repo.get_latest_by_symbol(run.id, "ETHUSDT")
        assert latest is not None
        assert latest.leverage_cap == 5


# ---------------------------------------------------------------------------
# FeaturePackageRepository
# ---------------------------------------------------------------------------


class TestFeaturePackageRepository:
    def test_get_by_hash(self, session: Session) -> None:
        run = _bot_run(session)
        h = _uid()
        pkg = FeaturePackage(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            version="v1",
            features={"x": 1},
            features_hash=h,
        )
        session.add(pkg)
        session.flush()

        repo = FeaturePackageRepository(session)
        found = repo.get_by_hash(h)
        assert found is not None
        assert found.features_hash == h

    def test_get_by_hash_missing(self, session: Session) -> None:
        repo = FeaturePackageRepository(session)
        assert repo.get_by_hash("nonexistent") is None


# ---------------------------------------------------------------------------
# ModelRequestRepository / ModelResponseRepository
# ---------------------------------------------------------------------------


class TestModelRequestRepository:
    def _model_request(self, session: Session, run: BotRun, hash_: str) -> ModelRequest:
        req = ModelRequest(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            model="gpt-4",
            context={"test": True},
            request_hash=hash_,
        )
        session.add(req)
        session.flush()
        return req

    def test_get_by_hash(self, session: Session) -> None:
        run = _bot_run(session)
        h = _uid()
        self._model_request(session, run, h)

        repo = ModelRequestRepository(session)
        found = repo.get_by_hash(h)
        assert found is not None
        assert found.request_hash == h

    def test_get_by_hash_missing(self, session: Session) -> None:
        repo = ModelRequestRepository(session)
        assert repo.get_by_hash("nonexistent") is None

    def test_list_by_bot_run(self, session: Session) -> None:
        run = _bot_run(session)
        self._model_request(session, run, _uid())
        self._model_request(session, run, _uid())

        repo = ModelRequestRepository(session)
        results = repo.list_by_bot_run(run.id)
        assert len(results) == 2


class TestModelResponseRepository:
    def _request_and_response(
        self, session: Session, run: BotRun
    ) -> tuple[ModelRequest, ModelResponse]:
        req = ModelRequest(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            model="gpt-4",
            context={},
            request_hash=_uid(),
        )
        session.add(req)
        session.flush()

        resp = ModelResponse(
            model_request_id=req.id,
            timestamp=_now(),
            raw_response='{"decision": "NO_OPERAR"}',
            model="gpt-4",
            is_valid_schema=True,
        )
        session.add(resp)
        session.flush()
        return req, resp

    def test_get_by_request_id(self, session: Session) -> None:
        run = _bot_run(session)
        req, resp = self._request_and_response(session, run)

        repo = ModelResponseRepository(session)
        found = repo.get_by_request_id(req.id)
        assert found is not None
        assert found.id == resp.id

    def test_get_by_request_id_missing(self, session: Session) -> None:
        repo = ModelResponseRepository(session)
        assert repo.get_by_request_id(_uid()) is None

    def test_list_by_bot_run_uses_join(self, session: Session) -> None:
        """list_by_bot_run hace JOIN via model_requests; verificar aislamiento por run."""
        run_a = _bot_run(session)
        run_b = _bot_run(session, status="STOPPED")
        self._request_and_response(session, run_a)
        self._request_and_response(session, run_a)
        self._request_and_response(session, run_b)

        repo = ModelResponseRepository(session)
        assert len(repo.list_by_bot_run(run_a.id)) == 2
        assert len(repo.list_by_bot_run(run_b.id)) == 1


# ---------------------------------------------------------------------------
# DecisionRepository
# ---------------------------------------------------------------------------


class TestDecisionRepository:
    def _decision(self, session: Session, run: BotRun, symbol: str, action: str) -> Decision:
        d = Decision(
            bot_run_id=run.id,
            symbol=symbol,
            timestamp=_now(),
            action=action,
        )
        session.add(d)
        session.flush()
        return d

    def test_list_by_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        self._decision(session, run, "BTCUSDT", "OPEN")
        self._decision(session, run, "ETHUSDT", "OPEN")
        self._decision(session, run, "BTCUSDT", "NO_OPERAR")

        repo = DecisionRepository(session)
        results = repo.list_by_symbol(run.id, "BTCUSDT")
        assert len(results) == 2

    def test_list_by_action(self, session: Session) -> None:
        run = _bot_run(session)
        self._decision(session, run, "BTCUSDT", "OPEN")
        self._decision(session, run, "BTCUSDT", "NO_OPERAR")

        repo = DecisionRepository(session)
        opens = repo.list_by_action(run.id, "OPEN")
        assert all(d.action == "OPEN" for d in opens)


# ---------------------------------------------------------------------------
# DecisionAggregationRepository
# ---------------------------------------------------------------------------


class TestDecisionAggregationRepository:
    def test_get_by_decision_id(self, session: Session) -> None:
        run = _bot_run(session)
        decision = Decision(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            action="OPEN",
        )
        session.add(decision)
        session.flush()

        agg = DecisionAggregation(
            bot_run_id=run.id,
            decision_id=decision.id,
            symbol="BTCUSDT",
            timestamp=_now(),
            final_action="OPEN",
        )
        session.add(agg)
        session.flush()

        repo = DecisionAggregationRepository(session)
        found = repo.get_by_decision_id(decision.id)
        assert found is not None
        assert found.final_action == "OPEN"


# ---------------------------------------------------------------------------
# RiskValidationRepository
# ---------------------------------------------------------------------------


class TestRiskValidationRepository:
    def test_list_by_result(self, session: Session) -> None:
        run = _bot_run(session)
        for result in ("APPROVE", "BLOCK", "APPROVE"):
            session.add(
                RiskValidation(
                    bot_run_id=run.id,
                    symbol="BTCUSDT",
                    timestamp=_now(),
                    result=result,
                )
            )
        session.flush()

        repo = RiskValidationRepository(session)
        approvals = repo.list_by_result(run.id, "APPROVE")
        assert len(approvals) == 2
        blocks = repo.list_by_result(run.id, "BLOCK")
        assert len(blocks) == 1


# ---------------------------------------------------------------------------
# TradeRepository
# ---------------------------------------------------------------------------


class TestTradeRepository:
    def _trade(
        self, session: Session, run: BotRun, symbol: str = "BTCUSDT", status: str = "OPEN"
    ) -> Trade:
        t = Trade(
            bot_run_id=run.id,
            symbol=symbol,
            environment="PAPER",
            direction="LONG",
            margin_usdt=Decimal("10"),
            leverage=5,
            status=status,
        )
        session.add(t)
        session.flush()
        return t

    def test_list_open(self, session: Session) -> None:
        run = _bot_run(session)
        self._trade(session, run, status="OPEN")
        self._trade(session, run, status="CLOSED")

        repo = TradeRepository(session)
        open_trades = repo.list_open(run.id)
        assert all(t.status == "OPEN" for t in open_trades)

    def test_list_by_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        self._trade(session, run, symbol="BTCUSDT")
        self._trade(session, run, symbol="ETHUSDT")

        repo = TradeRepository(session)
        btc = repo.list_by_symbol(run.id, "BTCUSDT")
        assert all(t.symbol == "BTCUSDT" for t in btc)

    def test_list_by_status(self, session: Session) -> None:
        run = _bot_run(session)
        self._trade(session, run, status="OPEN")
        self._trade(session, run, status="OPEN")
        self._trade(session, run, status="CLOSED")

        repo = TradeRepository(session)
        assert len(repo.list_by_status(run.id, "OPEN")) == 2
        assert len(repo.list_by_status(run.id, "CLOSED")) == 1


# ---------------------------------------------------------------------------
# OrderRepository
# ---------------------------------------------------------------------------


class TestOrderRepository:
    def _order(self, session: Session, run: BotRun, exchange_id: str | None = None) -> Order:
        o = Order(
            bot_run_id=run.id,
            client_order_id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            environment="PAPER",
            order_type="MARKET",
            side="BUY",
            quantity=Decimal("0.001"),
            status="PENDING",
            exchange_order_id=exchange_id,
        )
        session.add(o)
        session.flush()
        return o

    def test_get_by_exchange_id(self, session: Session) -> None:
        run = _bot_run(session)
        eid = _uid()
        self._order(session, run, exchange_id=eid)

        repo = OrderRepository(session)
        found = repo.get_by_exchange_id(eid)
        assert found is not None
        assert found.exchange_order_id == eid

    def test_get_by_exchange_id_missing(self, session: Session) -> None:
        repo = OrderRepository(session)
        assert repo.get_by_exchange_id("nonexistent") is None

    def test_list_by_status(self, session: Session) -> None:
        run = _bot_run(session)
        self._order(session, run)  # PENDING
        session.add(
            Order(
                bot_run_id=run.id,
                client_order_id=str(uuid.uuid4()),
                symbol="BTCUSDT",
                environment="PAPER",
                order_type="MARKET",
                side="SELL",
                quantity=Decimal("0.001"),
                status="FILLED",
            )
        )
        session.flush()

        repo = OrderRepository(session)
        assert len(repo.list_by_status(run.id, "PENDING")) >= 1
        assert len(repo.list_by_status(run.id, "FILLED")) >= 1

    def test_list_by_client_order_ids_returns_full_rows(self, session: Session) -> None:
        run = _bot_run(session)
        known = self._order(session, run)

        repo = OrderRepository(session)
        rows = repo.list_by_client_order_ids([known.client_order_id, "unknown-client-order-id"])

        assert [r.client_order_id for r in rows] == [known.client_order_id]
        assert rows[0].status == "PENDING"

    def test_list_by_client_order_ids_empty_input_returns_empty_without_querying(
        self, session: Session
    ) -> None:
        repo = OrderRepository(session)
        assert repo.list_by_client_order_ids([]) == []


# ---------------------------------------------------------------------------
# PositionRepository
# ---------------------------------------------------------------------------


class TestPositionRepository:
    def _position(
        self, session: Session, run: BotRun, symbol: str = "BTCUSDT", status: str = "OPEN"
    ) -> Position:
        p = Position(
            bot_run_id=run.id,
            symbol=symbol,
            environment="PAPER",
            direction="LONG",
            quantity=Decimal("0.001"),
            entry_price=Decimal("50000"),
            margin_usdt=Decimal("10"),
            leverage=5,
            status=status,
        )
        session.add(p)
        session.flush()
        return p

    def test_list_open(self, session: Session) -> None:
        run = _bot_run(session)
        self._position(session, run, status="OPEN")
        self._position(session, run, status="CLOSED")

        repo = PositionRepository(session)
        open_pos = repo.list_open(run.id)
        assert all(p.status == "OPEN" for p in open_pos)

    def test_get_open_by_symbol(self, session: Session) -> None:
        run = _bot_run(session)
        self._position(session, run, symbol="BTCUSDT", status="OPEN")

        repo = PositionRepository(session)
        pos = repo.get_open_by_symbol(run.id, "BTCUSDT")
        assert pos is not None
        assert pos.symbol == "BTCUSDT"

    def test_get_open_by_symbol_none(self, session: Session) -> None:
        run = _bot_run(session)
        repo = PositionRepository(session)
        assert repo.get_open_by_symbol(run.id, "SOLUSDT") is None


# ---------------------------------------------------------------------------
# PositionEventRepository
# ---------------------------------------------------------------------------


class TestPositionEventRepository:
    def test_list_by_position(self, session: Session) -> None:
        run = _bot_run(session)
        pos = Position(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            environment="PAPER",
            direction="LONG",
            quantity=Decimal("0.001"),
            entry_price=Decimal("50000"),
            margin_usdt=Decimal("10"),
            leverage=5,
        )
        session.add(pos)
        session.flush()

        for etype in ("SL_UPDATE", "TP_UPDATE"):
            session.add(PositionEvent(position_id=pos.id, event_type=etype))
        session.flush()

        repo = PositionEventRepository(session)
        events = repo.list_by_position(pos.id)
        assert len(events) == 2

    def test_list_by_event_type(self, session: Session) -> None:
        run = _bot_run(session)
        pos = Position(
            bot_run_id=run.id,
            symbol="ETHUSDT",
            environment="PAPER",
            direction="SHORT",
            quantity=Decimal("0.01"),
            entry_price=Decimal("3000"),
            margin_usdt=Decimal("10"),
            leverage=3,
        )
        session.add(pos)
        session.flush()

        session.add(PositionEvent(position_id=pos.id, event_type="TRAILING"))
        session.add(PositionEvent(position_id=pos.id, event_type="SL_UPDATE"))
        session.flush()

        repo = PositionEventRepository(session)
        trailing = repo.list_by_event_type(pos.id, "TRAILING")
        assert len(trailing) == 1


# ---------------------------------------------------------------------------
# SystemEventRepository
# ---------------------------------------------------------------------------


class TestSystemEventRepository:
    def test_list_by_severity(self, session: Session) -> None:
        run = _bot_run(session)
        for sev in ("INFO", "ERROR", "INFO"):
            session.add(
                SystemEvent(
                    bot_run_id=run.id,
                    event_type="TEST",
                    severity=sev,
                    message="msg",
                )
            )
        session.flush()

        repo = SystemEventRepository(session)
        infos = repo.list_by_severity(run.id, "INFO")
        assert len(infos) == 2

    def test_list_by_event_type(self, session: Session) -> None:
        run = _bot_run(session)
        session.add(
            SystemEvent(
                bot_run_id=run.id,
                event_type="KILL_SWITCH",
                severity="CRITICAL",
                message="kill",
            )
        )
        session.flush()

        repo = SystemEventRepository(session)
        results = repo.list_by_event_type(run.id, "KILL_SWITCH")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# ErrorRecordRepository
# ---------------------------------------------------------------------------


class TestErrorRecordRepository:
    def test_list_unrecovered(self, session: Session) -> None:
        run = _bot_run(session)
        session.add(ErrorRecord(bot_run_id=run.id, message="err1", recovered=False))
        session.add(ErrorRecord(bot_run_id=run.id, message="err2", recovered=True))
        session.flush()

        repo = ErrorRecordRepository(session)
        unrecovered = repo.list_unrecovered(run.id)
        assert all(not e.recovered for e in unrecovered)

    def test_list_by_module(self, session: Session) -> None:
        run = _bot_run(session)
        session.add(
            ErrorRecord(bot_run_id=run.id, message="err", module="risk_engine", recovered=False)
        )
        session.flush()

        repo = ErrorRecordRepository(session)
        results = repo.list_by_module(run.id, "risk_engine")
        assert len(results) == 1


# ---------------------------------------------------------------------------
# KillSwitchEventRepository
# ---------------------------------------------------------------------------


class TestKillSwitchEventRepository:
    def test_list_pending_review(self, session: Session) -> None:
        run = _bot_run(session)
        session.add(
            KillSwitchEvent(
                bot_run_id=run.id,
                trigger_reason="orphan orders",
                state_before="ACTIVE",
                action_taken="HALT",
                requires_manual_review=True,
            )
        )
        session.add(
            KillSwitchEvent(
                bot_run_id=run.id,
                trigger_reason="auto resolved",
                state_before="ACTIVE",
                action_taken="RESUME",
                requires_manual_review=False,
            )
        )
        session.flush()

        repo = KillSwitchEventRepository(session)
        pending = repo.list_pending_review(run.id)
        assert all(e.requires_manual_review for e in pending)


# ---------------------------------------------------------------------------
# TokenUsageRepository
# ---------------------------------------------------------------------------


class TestTokenUsageRepository:
    def test_total_tokens_for_run(self, session: Session) -> None:
        run = _bot_run(session)
        session.add(
            TokenUsage(
                bot_run_id=run.id,
                model="gpt-4",
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
            )
        )
        session.add(
            TokenUsage(
                bot_run_id=run.id,
                model="gpt-4",
                prompt_tokens=200,
                completion_tokens=100,
                total_tokens=300,
            )
        )
        session.flush()

        repo = TokenUsageRepository(session)
        total = repo.total_tokens_for_run(run.id)
        assert total == 450

    def test_total_tokens_empty(self, session: Session) -> None:
        run = _bot_run(session)
        repo = TokenUsageRepository(session)
        assert repo.total_tokens_for_run(run.id) == 0


# ---------------------------------------------------------------------------
# BacktestRunRepository + BacktestResultRepository
# ---------------------------------------------------------------------------


class TestBacktestRepositories:
    def test_backtest_run_list_by_status(self, session: Session) -> None:
        run = _bot_run(session)
        for status in ("RUNNING", "DONE", "RUNNING"):
            session.add(
                BacktestRun(
                    bot_run_id=run.id,
                    symbol="BTCUSDT",
                    period_start=datetime(2026, 1, 1, tzinfo=UTC),
                    period_end=datetime(2026, 2, 1, tzinfo=UTC),
                    config_snapshot={},
                    status=status,
                )
            )
        session.flush()

        repo = BacktestRunRepository(session)
        running = repo.list_by_status(run.id, "RUNNING")
        assert len(running) == 2

    def test_backtest_result_get_by_run(self, session: Session) -> None:
        run = _bot_run(session)
        bt_run = BacktestRun(
            bot_run_id=run.id,
            symbol="ETHUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 2, 1, tzinfo=UTC),
            config_snapshot={},
            status="DONE",
        )
        session.add(bt_run)
        session.flush()

        session.add(BacktestResult(backtest_run_id=bt_run.id, total_trades=10))
        session.flush()

        repo = BacktestResultRepository(session)
        results = repo.get_by_backtest_run(bt_run.id)
        assert len(results) == 1
        assert results[0].total_trades == 10


# ---------------------------------------------------------------------------
# HistoricalReplayRunRepository + HistoricalReplaySnapshotRepository
# ---------------------------------------------------------------------------


class TestHistoricalReplayRepositories:
    def test_replay_run_list_by_status(self, session: Session) -> None:
        run = _bot_run(session)
        session.add(
            HistoricalReplayRun(
                bot_run_id=run.id,
                symbol="BTCUSDT",
                period_start=datetime(2026, 1, 1, tzinfo=UTC),
                period_end=datetime(2026, 2, 1, tzinfo=UTC),
                config_snapshot={},
                status="DONE",
            )
        )
        session.flush()

        repo = HistoricalReplayRunRepository(session)
        done = repo.list_by_status(run.id, "DONE")
        assert len(done) == 1

    def test_replay_snapshot_list_by_replay_run(self, session: Session) -> None:
        run = _bot_run(session)
        replay = HistoricalReplayRun(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 2, 1, tzinfo=UTC),
            config_snapshot={},
            status="DONE",
        )
        session.add(replay)
        session.flush()

        for i in range(3):
            session.add(
                HistoricalReplaySnapshot(
                    replay_run_id=replay.id,
                    sequence_num=i,
                    market_snapshot={"seq": i},
                )
            )
        session.flush()

        repo = HistoricalReplaySnapshotRepository(session)
        snaps = repo.list_by_replay_run(replay.id)
        assert len(snaps) == 3
        assert [s.sequence_num for s in snaps] == [0, 1, 2]
