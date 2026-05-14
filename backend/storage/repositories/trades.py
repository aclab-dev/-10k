"""Repositorios para el agregado Trade: Trade, Order, Position, PositionEvent."""

from __future__ import annotations

from sqlalchemy import select

from backend.storage.models import Order, Position, PositionEvent, Trade
from backend.storage.repositories.base import BaseRepository


class TradeRepository(BaseRepository[Trade]):
    model = Trade

    def list_open(self, bot_run_id: str) -> list[Trade]:
        stmt = select(Trade).where(
            Trade.bot_run_id == bot_run_id,
            Trade.status == "OPEN",
        )
        return list(self._session.scalars(stmt))

    def list_by_symbol(
        self, bot_run_id: str, symbol: str, *, limit: int = 100
    ) -> list[Trade]:
        stmt = (
            select(Trade)
            .where(Trade.bot_run_id == bot_run_id, Trade.symbol == symbol)
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_by_status(
        self, bot_run_id: str, status: str, *, limit: int = 100
    ) -> list[Trade]:
        stmt = (
            select(Trade)
            .where(Trade.bot_run_id == bot_run_id, Trade.status == status)
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class OrderRepository(BaseRepository[Order]):
    model = Order

    def get_by_exchange_id(self, exchange_order_id: str) -> Order | None:
        stmt = select(Order).where(Order.exchange_order_id == exchange_order_id)
        return self._session.scalars(stmt).first()

    def list_by_trade(self, trade_id: str) -> list[Order]:
        stmt = select(Order).where(Order.trade_id == trade_id).order_by(Order.created_at)
        return list(self._session.scalars(stmt))

    def list_by_status(
        self, bot_run_id: str, status: str, *, limit: int = 100
    ) -> list[Order]:
        stmt = (
            select(Order)
            .where(Order.bot_run_id == bot_run_id, Order.status == status)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class PositionRepository(BaseRepository[Position]):
    model = Position

    def list_open(self, bot_run_id: str) -> list[Position]:
        stmt = select(Position).where(
            Position.bot_run_id == bot_run_id,
            Position.status == "OPEN",
        )
        return list(self._session.scalars(stmt))

    def get_open_by_symbol(self, bot_run_id: str, symbol: str) -> Position | None:
        """Devuelve la posición OPEN activa para el símbolo (ONE_WAY mode: máximo 1)."""
        stmt = (
            select(Position)
            .where(
                Position.bot_run_id == bot_run_id,
                Position.symbol == symbol,
                Position.status == "OPEN",
            )
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def get_by_trade_id(self, trade_id: str) -> Position | None:
        stmt = select(Position).where(Position.trade_id == trade_id)
        return self._session.scalars(stmt).first()


class PositionEventRepository(BaseRepository[PositionEvent]):
    model = PositionEvent

    def list_by_bot_run(
        self, bot_run_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[PositionEvent]:
        """PositionEvent no tiene bot_run_id; se une vía Position."""
        stmt = (
            select(PositionEvent)
            .join(Position, PositionEvent.position_id == Position.id)
            .where(Position.bot_run_id == bot_run_id)
            .order_by(PositionEvent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(stmt))

    def list_by_position(self, position_id: str) -> list[PositionEvent]:
        stmt = (
            select(PositionEvent)
            .where(PositionEvent.position_id == position_id)
            .order_by(PositionEvent.created_at)
        )
        return list(self._session.scalars(stmt))

    def list_by_event_type(
        self, position_id: str, event_type: str
    ) -> list[PositionEvent]:
        stmt = (
            select(PositionEvent)
            .where(
                PositionEvent.position_id == position_id,
                PositionEvent.event_type == event_type,
            )
            .order_by(PositionEvent.created_at)
        )
        return list(self._session.scalars(stmt))
