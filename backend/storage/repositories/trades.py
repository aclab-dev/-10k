"""Repositorios para el agregado Trade: Trade, Order, Position, PositionEvent."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from backend.storage.models import Order, Position, PositionEvent, Trade
from backend.storage.repositories.base import BaseRepository


class TradeRepository(BaseRepository[Trade]):
    model = Trade

    def get_loss_totals(self, bot_run_id: str) -> tuple[Decimal, Decimal]:
        """Retorna (daily_loss_usdt, total_loss_usdt) para este bot run.

        Ambos valores son >= 0 (solo pérdidas realizadas en USDT).
        daily_loss_usdt: sum(abs(net_pnl)) de trades CLOSED con pérdida hoy (UTC).
        total_loss_usdt: sum(abs(net_pnl)) de todos los trades CLOSED con pérdida.
        """
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

        daily_raw = self._session.scalar(
            select(func.sum(Trade.net_pnl)).where(
                Trade.bot_run_id == bot_run_id,
                Trade.status == "CLOSED",
                Trade.net_pnl < 0,
                Trade.closed_at >= today_start,
            )
        )
        total_raw = self._session.scalar(
            select(func.sum(Trade.net_pnl)).where(
                Trade.bot_run_id == bot_run_id,
                Trade.status == "CLOSED",
                Trade.net_pnl < 0,
            )
        )
        daily_loss = abs(daily_raw) if daily_raw is not None else Decimal("0")
        total_loss = abs(total_raw) if total_raw is not None else Decimal("0")
        return daily_loss, total_loss

    def get_last_closed_trade(self, bot_run_id: str, symbol: str) -> Trade | None:
        """Retorna el último trade cerrado del símbolo para este bot run."""
        stmt = (
            select(Trade)
            .where(
                Trade.bot_run_id == bot_run_id,
                Trade.symbol == symbol,
                Trade.status == "CLOSED",
                Trade.closed_at.is_not(None),
            )
            .order_by(Trade.closed_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def list_open(self, bot_run_id: str) -> list[Trade]:
        stmt = select(Trade).where(
            Trade.bot_run_id == bot_run_id,
            Trade.status == "OPEN",
        )
        return list(self._session.scalars(stmt))

    def list_by_symbol(self, bot_run_id: str, symbol: str, *, limit: int = 100) -> list[Trade]:
        stmt = (
            select(Trade)
            .where(Trade.bot_run_id == bot_run_id, Trade.symbol == symbol)
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_by_status(self, bot_run_id: str, status: str, *, limit: int = 100) -> list[Trade]:
        stmt = (
            select(Trade)
            .where(Trade.bot_run_id == bot_run_id, Trade.status == status)
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class OrderRepository(BaseRepository[Order]):
    model = Order

    def get_by_client_order_id(self, client_order_id: str) -> Order | None:
        stmt = select(Order).where(Order.client_order_id == client_order_id)
        return self._session.scalars(stmt).first()

    def get_by_exchange_id(self, exchange_order_id: str) -> Order | None:
        stmt = select(Order).where(Order.exchange_order_id == exchange_order_id)
        return self._session.scalars(stmt).first()

    def list_by_trade(self, trade_id: str) -> list[Order]:
        stmt = select(Order).where(Order.trade_id == trade_id).order_by(Order.created_at)
        return list(self._session.scalars(stmt))

    def list_by_status(self, bot_run_id: str, status: str, *, limit: int = 100) -> list[Order]:
        stmt = (
            select(Order)
            .where(Order.bot_run_id == bot_run_id, Order.status == status)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_known_client_order_ids(self, client_order_ids: list[str]) -> set[str]:
        """Subconjunto de `client_order_ids` que existen en la tabla, en una sola query.

        Pensado para reconciliacion contra el exchange (OrphanOrderScanner): evita
        N llamadas a get_by_client_order_id, una por orden activa devuelta por el
        adapter.
        """
        if not client_order_ids:
            return set()
        stmt = select(Order.client_order_id).where(Order.client_order_id.in_(client_order_ids))
        return set(self._session.scalars(stmt))


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

    def list_by_event_type(self, position_id: str, event_type: str) -> list[PositionEvent]:
        stmt = (
            select(PositionEvent)
            .where(
                PositionEvent.position_id == position_id,
                PositionEvent.event_type == event_type,
            )
            .order_by(PositionEvent.created_at)
        )
        return list(self._session.scalars(stmt))
