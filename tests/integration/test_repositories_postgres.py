"""Tests de integración: repositorios contra PostgreSQL real.

Verifican constraints NOT NULL y lógica de consulta que SQLite in-memory
no puede validar correctamente. Todos los tests usan el contenedor de
Postgres levantado por los fixtures en conftest.py.

Ejecutar con: pytest -m integration
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.storage.models import (
    BotRun,
    Decision,
    Order,
    Position,
    Trade,
)
from backend.storage.repositories import (
    BotRunRepository,
    DecisionRepository,
    OrderRepository,
    PositionRepository,
    TradeRepository,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _bot_run(session: Session) -> BotRun:
    run = BotRun(
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"test": True},
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    return run


def _trade(session: Session, bot_run: BotRun) -> Trade:
    trade = Trade(
        bot_run_id=bot_run.id,
        symbol="BTCUSDT",
        environment="PAPER",
        direction="LONG",
        margin_usdt=Decimal("10"),
        leverage=3,
    )
    session.add(trade)
    session.flush()
    return trade


# ---------------------------------------------------------------------------
# BotRun — constraints NOT NULL
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBotRunConstraints:
    def test_missing_environment_raises(self, pg_session: Session) -> None:
        """environment es NOT NULL — Postgres debe rechazar la inserción."""
        run = BotRun(app_version="0.1.0", config_snapshot={}, status="RUNNING")
        pg_session.add(run)
        with pytest.raises(IntegrityError, match='null value in column "environment"'):
            pg_session.flush()

    def test_missing_app_version_raises(self, pg_session: Session) -> None:
        """app_version es NOT NULL."""
        run = BotRun(environment="PAPER", config_snapshot={}, status="RUNNING")
        pg_session.add(run)
        with pytest.raises(IntegrityError, match='null value in column "app_version"'):
            pg_session.flush()

    def test_config_snapshot_jsonb_roundtrip(self, pg_session: Session) -> None:
        """config_snapshot se persiste como JSONB y se recupera fielmente en Postgres.

        Nota: Python None → JSON null (no SQL NULL) en columnas JSONB de SQLAlchemy,
        por lo que el constraint NOT NULL no aplica a None de Python. Este test
        verifica el comportamiento real de JSONB en Postgres.
        """
        payload = {"environment": "PAPER", "leverage": 3, "symbols": ["BTCUSDT"]}
        run = BotRun(
            environment="PAPER",
            app_version="0.1.0",
            config_snapshot=payload,
            status="RUNNING",
        )
        pg_session.add(run)
        pg_session.flush()
        pg_session.expire(run)
        assert run.config_snapshot == payload


# ---------------------------------------------------------------------------
# BotRun — CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBotRunRepositoryPostgres:
    def test_save_and_get(self, pg_session: Session) -> None:
        repo = BotRunRepository(pg_session)
        run = _bot_run(pg_session)
        fetched = repo.get_by_id(run.id)
        assert fetched is not None
        assert fetched.environment == "PAPER"
        assert fetched.app_version == "0.1.0"

    def test_get_active_returns_running(self, pg_session: Session) -> None:
        repo = BotRunRepository(pg_session)
        run = _bot_run(pg_session)
        active = repo.get_active()
        assert active is not None
        assert active.id == run.id

    def test_close_sets_status_and_ended_at(self, pg_session: Session) -> None:
        repo = BotRunRepository(pg_session)
        run = _bot_run(pg_session)
        repo.close(run, status="STOPPED")
        assert run.status == "STOPPED"
        assert run.ended_at is not None

    def test_get_by_id_missing(self, pg_session: Session) -> None:
        repo = BotRunRepository(pg_session)
        assert repo.get_by_id(str(uuid.uuid4())) is None


# ---------------------------------------------------------------------------
# Decision — constraints NOT NULL
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDecisionConstraints:
    def test_missing_symbol_raises(self, pg_session: Session) -> None:
        """symbol es NOT NULL en decisions."""
        run = _bot_run(pg_session)
        decision = Decision(
            bot_run_id=run.id,
            timestamp=_now(),
            action="NO_OPERAR",
        )
        pg_session.add(decision)
        with pytest.raises(IntegrityError, match='null value in column "symbol"'):
            pg_session.flush()

    def test_missing_action_raises(self, pg_session: Session) -> None:
        """action es NOT NULL en decisions."""
        run = _bot_run(pg_session)
        decision = Decision(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            timestamp=_now(),
        )
        pg_session.add(decision)
        with pytest.raises(IntegrityError, match='null value in column "action"'):
            pg_session.flush()

    def test_missing_bot_run_fk_raises(self, pg_session: Session) -> None:
        """bot_run_id FK inexistente — Postgres lo rechaza con FK violation."""
        decision = Decision(
            bot_run_id=str(uuid.uuid4()),
            symbol="BTCUSDT",
            timestamp=_now(),
            action="NO_OPERAR",
        )
        pg_session.add(decision)
        with pytest.raises(IntegrityError, match="foreign key"):
            pg_session.flush()


# ---------------------------------------------------------------------------
# Decision — CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestDecisionRepositoryPostgres:
    def test_list_by_symbol(self, pg_session: Session) -> None:
        repo = DecisionRepository(pg_session)
        run = _bot_run(pg_session)
        for symbol in ("BTCUSDT", "BTCUSDT", "ETHUSDT"):
            pg_session.add(
                Decision(
                    bot_run_id=run.id,
                    symbol=symbol,
                    timestamp=_now(),
                    action="NO_OPERAR",
                )
            )
        pg_session.flush()
        results = repo.list_by_symbol(run.id, "BTCUSDT")
        assert len(results) == 2

    def test_list_by_action(self, pg_session: Session) -> None:
        repo = DecisionRepository(pg_session)
        run = _bot_run(pg_session)
        for action in ("OPEN", "OPEN", "NO_OPERAR"):
            pg_session.add(
                Decision(
                    bot_run_id=run.id,
                    symbol="BTCUSDT",
                    timestamp=_now(),
                    action=action,
                )
            )
        pg_session.flush()
        results = repo.list_by_action(run.id, "OPEN")
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Trade — constraints NOT NULL
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTradeConstraints:
    def test_missing_symbol_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        trade = Trade(
            bot_run_id=run.id,
            environment="PAPER",
            direction="LONG",
            margin_usdt=Decimal("10"),
            leverage=3,
        )
        pg_session.add(trade)
        with pytest.raises(IntegrityError, match='null value in column "symbol"'):
            pg_session.flush()

    def test_missing_environment_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        trade = Trade(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            direction="LONG",
            margin_usdt=Decimal("10"),
            leverage=3,
        )
        pg_session.add(trade)
        with pytest.raises(IntegrityError, match='null value in column "environment"'):
            pg_session.flush()

    def test_missing_direction_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        trade = Trade(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            environment="PAPER",
            margin_usdt=Decimal("10"),
            leverage=3,
        )
        pg_session.add(trade)
        with pytest.raises(IntegrityError, match='null value in column "direction"'):
            pg_session.flush()

    def test_missing_margin_usdt_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        trade = Trade(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            environment="PAPER",
            direction="LONG",
            leverage=3,
        )
        pg_session.add(trade)
        with pytest.raises(IntegrityError, match='null value in column "margin_usdt"'):
            pg_session.flush()

    def test_missing_leverage_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        trade = Trade(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            environment="PAPER",
            direction="LONG",
            margin_usdt=Decimal("10"),
        )
        pg_session.add(trade)
        with pytest.raises(IntegrityError, match='null value in column "leverage"'):
            pg_session.flush()


# ---------------------------------------------------------------------------
# Trade — CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestTradeRepositoryPostgres:
    def test_list_open(self, pg_session: Session) -> None:
        repo = TradeRepository(pg_session)
        run = _bot_run(pg_session)
        _trade(pg_session, run)
        results = repo.list_open(run.id)
        assert len(results) == 1
        assert results[0].status == "OPEN"

    def test_list_by_symbol(self, pg_session: Session) -> None:
        repo = TradeRepository(pg_session)
        run = _bot_run(pg_session)
        for symbol in ("BTCUSDT", "ETHUSDT"):
            pg_session.add(
                Trade(
                    bot_run_id=run.id,
                    symbol=symbol,
                    environment="PAPER",
                    direction="LONG",
                    margin_usdt=Decimal("5"),
                    leverage=2,
                )
            )
        pg_session.flush()
        btc = repo.list_by_symbol(run.id, "BTCUSDT")
        assert len(btc) == 1

    def test_list_by_status(self, pg_session: Session) -> None:
        repo = TradeRepository(pg_session)
        run = _bot_run(pg_session)
        t1 = _trade(pg_session, run)
        t2 = _trade(pg_session, run)
        t2.status = "CLOSED"
        pg_session.flush()
        open_trades = repo.list_by_status(run.id, "OPEN")
        assert any(t.id == t1.id for t in open_trades)
        assert all(t.id != t2.id for t in open_trades)


# ---------------------------------------------------------------------------
# Order — constraints NOT NULL
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOrderConstraints:
    def test_missing_symbol_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        order = Order(
            bot_run_id=run.id,
            environment="PAPER",
            order_type="MARKET",
            side="BUY",
            quantity=Decimal("0.001"),
        )
        pg_session.add(order)
        with pytest.raises(IntegrityError, match='null value in column "symbol"'):
            pg_session.flush()

    def test_missing_quantity_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        order = Order(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            environment="PAPER",
            order_type="MARKET",
            side="BUY",
        )
        pg_session.add(order)
        with pytest.raises(IntegrityError, match='null value in column "quantity"'):
            pg_session.flush()


# ---------------------------------------------------------------------------
# Order — CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOrderRepositoryPostgres:
    def test_list_by_status(self, pg_session: Session) -> None:
        repo = OrderRepository(pg_session)
        run = _bot_run(pg_session)
        for status in ("PENDING", "PENDING", "FILLED"):
            pg_session.add(
                Order(
                    bot_run_id=run.id,
                    symbol="BTCUSDT",
                    environment="PAPER",
                    order_type="MARKET",
                    side="BUY",
                    quantity=Decimal("0.001"),
                    status=status,
                )
            )
        pg_session.flush()
        pending = repo.list_by_status(run.id, "PENDING")
        assert len(pending) == 2

    def test_get_by_exchange_id(self, pg_session: Session) -> None:
        repo = OrderRepository(pg_session)
        run = _bot_run(pg_session)
        order = Order(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            environment="PAPER",
            order_type="MARKET",
            side="BUY",
            quantity=Decimal("0.001"),
            exchange_order_id="EX-12345",
        )
        pg_session.add(order)
        pg_session.flush()
        fetched = repo.get_by_exchange_id("EX-12345")
        assert fetched is not None
        assert fetched.id == order.id

    def test_get_by_exchange_id_missing(self, pg_session: Session) -> None:
        repo = OrderRepository(pg_session)
        assert repo.get_by_exchange_id("NONEXISTENT") is None


# ---------------------------------------------------------------------------
# Position — constraints NOT NULL
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPositionConstraints:
    def test_missing_direction_raises(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        pos = Position(
            bot_run_id=run.id,
            symbol="BTCUSDT",
            environment="PAPER",
            quantity=Decimal("0.001"),
            entry_price=Decimal("50000"),
            margin_usdt=Decimal("10"),
            leverage=3,
        )
        pg_session.add(pos)
        with pytest.raises(IntegrityError, match='null value in column "direction"'):
            pg_session.flush()


# ---------------------------------------------------------------------------
# Position — CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPositionRepositoryPostgres:
    def test_list_open(self, pg_session: Session) -> None:
        repo = PositionRepository(pg_session)
        run = _bot_run(pg_session)
        pg_session.add(
            Position(
                bot_run_id=run.id,
                symbol="BTCUSDT",
                environment="PAPER",
                direction="LONG",
                quantity=Decimal("0.001"),
                entry_price=Decimal("50000"),
                margin_usdt=Decimal("10"),
                leverage=3,
            )
        )
        pg_session.flush()
        results = repo.list_open(run.id)
        assert len(results) == 1

    def test_get_open_by_symbol(self, pg_session: Session) -> None:
        repo = PositionRepository(pg_session)
        run = _bot_run(pg_session)
        pg_session.add(
            Position(
                bot_run_id=run.id,
                symbol="ETHUSDT",
                environment="PAPER",
                direction="SHORT",
                quantity=Decimal("0.01"),
                entry_price=Decimal("2000"),
                margin_usdt=Decimal("10"),
                leverage=2,
            )
        )
        pg_session.flush()
        pos = repo.get_open_by_symbol(run.id, "ETHUSDT")
        assert pos is not None
        assert pos.direction == "SHORT"

    def test_get_open_by_symbol_none(self, pg_session: Session) -> None:
        repo = PositionRepository(pg_session)
        run = _bot_run(pg_session)
        assert repo.get_open_by_symbol(run.id, "BNBUSDT") is None
