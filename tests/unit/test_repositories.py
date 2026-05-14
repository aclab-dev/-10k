"""Tests unitarios de repositorios — SQLite in-memory.

Cubre CRUD y queries críticas de BotRunRepository, TradeRepository,
PositionRepository y OrderRepository.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.storage.models import BotRun, Trade
from backend.storage.repositories import (
    BotRunRepository,
    OrderRepository,
    PositionRepository,
    TradeRepository,
)


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _make_bot_run(session: Session, status: str = "RUNNING") -> BotRun:
    return BotRunRepository().create(
        session,
        id=_uid(),
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"mode": "PAPER"},
        status=status,
    )


def _make_trade(session: Session, bot_run: BotRun, symbol: str = "BTCUSDT") -> Trade:
    return TradeRepository().create(
        session,
        id=_uid(),
        bot_run_id=bot_run.id,
        symbol=symbol,
        environment="PAPER",
        direction="LONG",
        margin_usdt=Decimal("10"),
        leverage=3,
        opened_at=_now(),
    )


class TestBotRunRepository:
    def test_create_returns_instance(self, session):
        run = _make_bot_run(session)
        assert run.id is not None
        assert run.status == "RUNNING"

    def test_get_by_id_existing(self, session):
        run = _make_bot_run(session)
        fetched = BotRunRepository().get_by_id(session, run.id)
        assert fetched is not None
        assert fetched.id == run.id

    def test_get_by_id_missing_returns_none(self, session):
        assert BotRunRepository().get_by_id(session, _uid()) is None

    def test_get_active_runs_only_running(self, session):
        repo = BotRunRepository()
        running = _make_bot_run(session, status="RUNNING")
        _make_bot_run(session, status="STOPPED")
        active = repo.get_active_runs(session)
        ids = [r.id for r in active]
        assert running.id in ids
        for r in active:
            assert r.status == "RUNNING"

    def test_mark_finished_changes_status(self, session):
        run = _make_bot_run(session)
        updated = BotRunRepository().mark_finished(session, run.id, "STOPPED")
        assert updated is not None
        assert updated.status == "STOPPED"

    def test_mark_finished_missing_returns_none(self, session):
        assert BotRunRepository().mark_finished(session, _uid(), "STOPPED") is None

    def test_create_persists_config_snapshot(self, session):
        repo = BotRunRepository()
        run = repo.create(
            session,
            id=_uid(),
            environment="TESTNET",
            app_version="0.2.0",
            config_snapshot={"leverage_cap": 5, "symbols": ["BTCUSDT", "ETHUSDT"]},
        )
        fetched = repo.get_by_id(session, run.id)
        assert fetched is not None
        assert fetched.config_snapshot["leverage_cap"] == 5


class TestTradeRepository:
    def test_create_returns_instance(self, session):
        run = _make_bot_run(session)
        trade = _make_trade(session, run)
        assert trade.id is not None
        assert trade.status == "OPEN"

    def test_get_by_id_existing(self, session):
        run = _make_bot_run(session)
        trade = _make_trade(session, run)
        fetched = TradeRepository().get_by_id(session, trade.id)
        assert fetched is not None
        assert fetched.id == trade.id

    def test_get_by_id_missing_returns_none(self, session):
        assert TradeRepository().get_by_id(session, _uid()) is None

    def test_get_open_trades_only_open(self, session):
        repo = TradeRepository()
        run = _make_bot_run(session)
        open_trade = _make_trade(session, run, symbol="BTCUSDT")
        closed_trade = _make_trade(session, run, symbol="ETHUSDT")
        repo.close_trade(session, closed_trade.id, Decimal("3100"), Decimal("5"), "SL")

        open_trades = repo.get_open_trades(session, run.id)
        ids = [t.id for t in open_trades]
        assert open_trade.id in ids
        assert closed_trade.id not in ids

    def test_get_by_symbol_filters_correctly(self, session):
        repo = TradeRepository()
        run = _make_bot_run(session)
        btc_trade = _make_trade(session, run, symbol="BTCUSDT")
        _make_trade(session, run, symbol="ETHUSDT")

        result = repo.get_by_symbol(session, run.id, "BTCUSDT")
        ids = [t.id for t in result]
        assert btc_trade.id in ids
        for t in result:
            assert t.symbol == "BTCUSDT"

    def test_get_by_symbol_different_bot_run_isolated(self, session):
        repo = TradeRepository()
        run_a = _make_bot_run(session)
        run_b = _make_bot_run(session)
        _make_trade(session, run_a, symbol="BTCUSDT")
        _make_trade(session, run_b, symbol="BTCUSDT")

        result = repo.get_by_symbol(session, run_a.id, "BTCUSDT")
        for t in result:
            assert t.bot_run_id == run_a.id

    def test_close_trade_sets_fields(self, session):
        run = _make_bot_run(session)
        trade = _make_trade(session, run)
        updated = TradeRepository().close_trade(
            session, trade.id, Decimal("62000"), Decimal("8.5"), "TP"
        )
        assert updated is not None
        assert updated.status == "CLOSED"
        assert updated.exit_price == Decimal("62000")
        assert updated.net_pnl == Decimal("8.5")
        assert updated.close_reason == "TP"

    def test_close_trade_missing_returns_none(self, session):
        result = TradeRepository().close_trade(
            session, _uid(), Decimal("1"), Decimal("0"), "SL"
        )
        assert result is None


class TestPositionRepository:
    def _make_position(self, session: Session, bot_run: BotRun, symbol: str = "BTCUSDT"):
        return PositionRepository().create(
            session,
            id=_uid(),
            bot_run_id=bot_run.id,
            symbol=symbol,
            environment="PAPER",
            direction="LONG",
            quantity=Decimal("0.001"),
            entry_price=Decimal("60000"),
            margin_usdt=Decimal("10"),
            leverage=3,
            opened_at=_now(),
            updated_at=_now(),
        )

    def test_create_returns_instance(self, session):
        run = _make_bot_run(session)
        pos = self._make_position(session, run)
        assert pos.id is not None
        assert pos.status == "OPEN"

    def test_get_by_id_existing(self, session):
        run = _make_bot_run(session)
        pos = self._make_position(session, run)
        fetched = PositionRepository().get_by_id(session, pos.id)
        assert fetched is not None
        assert fetched.id == pos.id

    def test_get_by_id_missing_returns_none(self, session):
        assert PositionRepository().get_by_id(session, _uid()) is None

    def test_get_open_positions_only_open(self, session):
        repo = PositionRepository()
        run = _make_bot_run(session)
        open_pos = self._make_position(session, run, symbol="BTCUSDT")
        closed_pos = self._make_position(session, run, symbol="ETHUSDT")
        closed_pos.status = "CLOSED"
        session.flush()

        open_positions = repo.get_open_positions(session, run.id)
        ids = [p.id for p in open_positions]
        assert open_pos.id in ids
        assert closed_pos.id not in ids

    def test_get_by_symbol_returns_open_position(self, session):
        run = _make_bot_run(session)
        pos = self._make_position(session, run, symbol="SOLUSDT")
        result = PositionRepository().get_by_symbol(session, run.id, "SOLUSDT")
        assert result is not None
        assert result.id == pos.id

    def test_get_by_symbol_no_open_returns_none(self, session):
        run = _make_bot_run(session)
        assert PositionRepository().get_by_symbol(session, run.id, "XRPUSDT") is None

    def test_update_price_changes_fields(self, session):
        run = _make_bot_run(session)
        pos = self._make_position(session, run)
        updated = PositionRepository().update_price(
            session, pos.id, Decimal("62000"), Decimal("2.0")
        )
        assert updated is not None
        assert updated.current_price == Decimal("62000")
        assert updated.unrealized_pnl == Decimal("2.0")

    def test_update_price_missing_returns_none(self, session):
        result = PositionRepository().update_price(
            session, _uid(), Decimal("1"), Decimal("0")
        )
        assert result is None


class TestOrderRepository:
    def _make_order(
        self,
        session: Session,
        bot_run: BotRun,
        trade_id: str | None = None,
        status: str = "PENDING",
    ):
        return OrderRepository().create(
            session,
            id=_uid(),
            bot_run_id=bot_run.id,
            trade_id=trade_id,
            symbol="BTCUSDT",
            environment="PAPER",
            order_type="MARKET",
            side="BUY",
            quantity=Decimal("0.001"),
            status=status,
            is_simulated=True,
            created_at=_now(),
        )

    def test_create_returns_instance(self, session):
        run = _make_bot_run(session)
        order = self._make_order(session, run)
        assert order.id is not None
        assert order.is_simulated is True

    def test_get_by_id_existing(self, session):
        run = _make_bot_run(session)
        order = self._make_order(session, run)
        fetched = OrderRepository().get_by_id(session, order.id)
        assert fetched is not None
        assert fetched.id == order.id

    def test_get_by_id_missing_returns_none(self, session):
        assert OrderRepository().get_by_id(session, _uid()) is None

    def test_get_by_trade_id(self, session):
        repo = OrderRepository()
        run = _make_bot_run(session)
        trade = _make_trade(session, run)
        o1 = self._make_order(session, run, trade_id=trade.id)
        o2 = self._make_order(session, run, trade_id=trade.id)
        self._make_order(session, run, trade_id=None)

        result = repo.get_by_trade_id(session, trade.id)
        ids = [o.id for o in result]
        assert o1.id in ids
        assert o2.id in ids
        assert all(o.trade_id == trade.id for o in result)

    def test_get_pending_orders_only_pending(self, session):
        repo = OrderRepository()
        run = _make_bot_run(session)
        pending = self._make_order(session, run, status="PENDING")
        filled = self._make_order(session, run, status="FILLED")

        result = repo.get_pending_orders(session, run.id)
        ids = [o.id for o in result]
        assert pending.id in ids
        assert filled.id not in ids

    def test_fill_order_sets_fields(self, session):
        run = _make_bot_run(session)
        order = self._make_order(session, run)
        updated = OrderRepository().fill_order(
            session, order.id, Decimal("60500"), Decimal("0.024")
        )
        assert updated is not None
        assert updated.status == "FILLED"
        assert updated.fill_price == Decimal("60500")
        assert updated.fee == Decimal("0.024")

    def test_fill_order_missing_returns_none(self, session):
        result = OrderRepository().fill_order(
            session, _uid(), Decimal("1"), Decimal("0")
        )
        assert result is None
