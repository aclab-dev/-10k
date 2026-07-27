"""E2E: detección automática de invalidación de setup + persistencia en position_events.

Tarjeta [106] [F14]. Ejercita el flujo completo contra Postgres real: abrir una
posición (PaperAdapter + fila Position), configurar invalidation_price/action en
PositionManager, cruzar el precio en tick() y verificar que on_invalidation_event
persiste una fila en position_events (event_type="INVALIDATION") vía
PositionEventRepository.

Ejecutar con: pytest -m integration
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.position_manager import (
    InvalidationAction,
    InvalidationEvent,
    PositionConfig,
    PositionManager,
    PositionTriggerReason,
)
from backend.storage.models import BotRun, Position, PositionEvent
from backend.storage.repositories import PositionEventRepository

SYMBOL = "BTCUSDT"


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


def _open_long_position_row(session: Session, bot_run: BotRun) -> Position:
    """Crea la fila Position (DB) para la posición LONG abierta en PaperAdapter."""
    pos = Position(
        bot_run_id=bot_run.id,
        symbol=SYMBOL,
        environment="PAPER",
        direction="LONG",
        quantity=Decimal("1"),
        entry_price=Decimal("50000"),
        margin_usdt=Decimal("10"),
        leverage=5,
        stop_loss=Decimal("45000"),
        status="OPEN",
    )
    session.add(pos)
    session.flush()
    return pos


@pytest.mark.integration
class TestSetupInvalidationE2E:
    def test_invalidation_price_crossed_closes_position_and_persists_event(
        self, pg_session: Session
    ) -> None:
        bot_run = _bot_run(pg_session)
        position_row = _open_long_position_row(pg_session, bot_run)
        event_repo = PositionEventRepository(pg_session)

        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        adapter.set_leverage(SYMBOL, 5)
        adapter.place_order(
            OrderRequest(
                symbol=SYMBOL,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                price=Decimal("50000"),
            )
        )

        def _on_invalidation_event(event: InvalidationEvent) -> None:
            event_repo.save(
                PositionEvent(
                    position_id=position_row.id,
                    event_type="INVALIDATION",
                    old_value=event.old_sl,
                    new_value=event.new_sl,
                    reason=(
                        f"mark_price={event.mark_price} closed_fraction={event.closed_fraction}"
                    ),
                )
            )

        pm = PositionManager(adapter, on_invalidation_event=_on_invalidation_event)
        pm.set_config(
            PositionConfig(
                symbol=SYMBOL,
                stop_loss=Decimal("45000"),
                invalidation_price=Decimal("49000"),
                invalidation_action=InvalidationAction(close_fraction=Decimal("1")),
            )
        )

        result = pm.tick(SYMBOL, Decimal("48900"))

        assert result.trigger == PositionTriggerReason.SETUP_INVALIDATED
        assert adapter.get_position(SYMBOL) is None

        persisted = event_repo.list_by_position(position_row.id)
        assert len(persisted) == 1
        assert persisted[0].event_type == "INVALIDATION"
        assert persisted[0].old_value == Decimal("45000")
        assert persisted[0].new_value is None

    def test_invalidation_price_crossed_moves_sl_persists_event_position_stays_open(
        self, pg_session: Session
    ) -> None:
        bot_run = _bot_run(pg_session)
        position_row = _open_long_position_row(pg_session, bot_run)
        event_repo = PositionEventRepository(pg_session)

        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        adapter.set_leverage(SYMBOL, 5)
        adapter.place_order(
            OrderRequest(
                symbol=SYMBOL,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                price=Decimal("50000"),
            )
        )

        def _on_invalidation_event(event: InvalidationEvent) -> None:
            event_repo.save(
                PositionEvent(
                    position_id=position_row.id,
                    event_type="INVALIDATION",
                    old_value=event.old_sl,
                    new_value=event.new_sl,
                    reason=f"mark_price={event.mark_price}",
                )
            )

        pm = PositionManager(adapter, on_invalidation_event=_on_invalidation_event)
        pm.set_config(
            PositionConfig(
                symbol=SYMBOL,
                stop_loss=Decimal("45000"),
                invalidation_price=Decimal("49000"),
                invalidation_action=InvalidationAction(new_sl=Decimal("49500")),
            )
        )

        result = pm.tick(SYMBOL, Decimal("48900"))

        assert result.trigger == PositionTriggerReason.SETUP_INVALIDATED
        assert adapter.get_position(SYMBOL) is not None
        assert pm.get_effective_sl(SYMBOL) == Decimal("49500")

        persisted = event_repo.list_by_position(position_row.id)
        assert len(persisted) == 1
        assert persisted[0].event_type == "INVALIDATION"
        assert persisted[0].old_value == Decimal("45000")
        assert persisted[0].new_value == Decimal("49500")
