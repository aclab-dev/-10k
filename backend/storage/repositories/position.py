from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from backend.storage.models import Position


class PositionRepository:
    def create(self, session: Session, **kwargs: Any) -> Position:
        pos = Position(**kwargs)
        session.add(pos)
        session.flush()
        return pos

    def get_by_id(self, session: Session, position_id: str) -> Position | None:
        return session.get(Position, position_id)

    def get_open_positions(self, session: Session, bot_run_id: str) -> list[Position]:
        return (
            session.query(Position)
            .filter(Position.bot_run_id == bot_run_id, Position.status == "OPEN")
            .all()
        )

    def get_by_symbol(
        self, session: Session, bot_run_id: str, symbol: str
    ) -> Position | None:
        return (
            session.query(Position)
            .filter(
                Position.bot_run_id == bot_run_id,
                Position.symbol == symbol,
                Position.status == "OPEN",
            )
            .first()
        )

    def update_price(
        self,
        session: Session,
        position_id: str,
        current_price: Decimal,
        unrealized_pnl: Decimal,
    ) -> Position | None:
        pos = session.get(Position, position_id)
        if pos is None:
            return None
        pos.current_price = current_price
        pos.unrealized_pnl = unrealized_pnl
        session.flush()
        return pos
