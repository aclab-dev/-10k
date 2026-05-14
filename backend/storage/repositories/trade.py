from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.storage.models import Trade
from backend.storage.repositories.base import BaseRepository


class TradeRepository(BaseRepository[Trade]):
    _model = Trade

    def get_by_id(self, session: Session, trade_id: str) -> Trade | None:
        return session.get(Trade, trade_id)

    def get_open_trades(self, session: Session, bot_run_id: str) -> list[Trade]:
        return session.execute(
            select(Trade).where(Trade.bot_run_id == bot_run_id, Trade.status == "OPEN")
        ).scalars().all()  # type: ignore[return-value]

    def get_by_symbol(self, session: Session, bot_run_id: str, symbol: str) -> list[Trade]:
        return session.execute(
            select(Trade).where(Trade.bot_run_id == bot_run_id, Trade.symbol == symbol)
        ).scalars().all()  # type: ignore[return-value]

    def close_trade(
        self,
        session: Session,
        trade_id: str,
        exit_price: Decimal,
        net_pnl: Decimal,
        close_reason: str,
    ) -> Trade | None:
        trade = session.get(Trade, trade_id)
        if trade is None:
            return None
        trade.exit_price = exit_price
        trade.net_pnl = net_pnl
        trade.close_reason = close_reason
        trade.status = "CLOSED"
        session.flush()
        return trade
