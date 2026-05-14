from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from backend.storage.models import Order


class OrderRepository:
    def create(self, session: Session, **kwargs: Any) -> Order:
        order = Order(**kwargs)
        session.add(order)
        session.flush()
        return order

    def get_by_id(self, session: Session, order_id: str) -> Order | None:
        return session.get(Order, order_id)

    def get_by_trade_id(self, session: Session, trade_id: str) -> list[Order]:
        return session.query(Order).filter(Order.trade_id == trade_id).all()

    def get_pending_orders(self, session: Session, bot_run_id: str) -> list[Order]:
        return (
            session.query(Order)
            .filter(Order.bot_run_id == bot_run_id, Order.status == "PENDING")
            .all()
        )

    def fill_order(
        self,
        session: Session,
        order_id: str,
        fill_price: Decimal,
        fee: Decimal,
    ) -> Order | None:
        order = session.get(Order, order_id)
        if order is None:
            return None
        order.fill_price = fill_price
        order.fee = fee
        order.status = "FILLED"
        session.flush()
        return order
