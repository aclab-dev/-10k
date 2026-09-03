"""Caos — integración a nivel CycleRunner (F16 [118]).

Arma un `CycleRunner._tick()` con el monitor de conexión y el scanner de
órdenes huérfanas reales y comprueba que un fallo detectado en cualquiera de
ellos deja el ciclo en SAFE_MODE: no se abren posiciones nuevas, pero las
salidas se siguen gestionando.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from backend.connection_health.monitor import ConnectionHealthMonitor
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.fetcher import MockDataFetcher
from backend.orphan_order_scanner.scanner import OrphanOrderScanner
from backend.position_manager.manager import PositionManager
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import CycleRunner
from tests.chaos.faults import ChaosAdapter
from tests.unit.conftest import make_bot_run

pytestmark = pytest.mark.chaos


@pytest.fixture
def heartbeat_file(tmp_path: Path) -> Path:
    return tmp_path / "worker_alive"


def _snapshots(*symbols: str) -> list[object]:
    fetcher = MockDataFetcher(seed=7)

    async def _build() -> list[object]:
        return [await fetcher.fetch_snapshot(s, Decimal("1000")) for s in symbols]

    return asyncio.run(_build())


def test_tick_with_dead_market_data_ends_in_safe_mode_but_keeps_managing_exits(
    session: Session, heartbeat_file: Path
) -> None:
    bot_run = make_bot_run(session, status="RUNNING")
    sm = BotStateMachine(initial=BotState.ACTIVE)

    market_data_service = Mock(spec=MarketDataCycleService)
    market_data_service.tick_all.return_value = []  # exchange caído: cero snapshots
    position_tick_service = Mock()
    monitor = ConnectionHealthMonitor(
        state_machine=sm,
        session=session,
        bot_run_id=bot_run.id,
        max_clock_skew_ms=2000,
        max_latency_ms=3000,
        symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        market_data_service=market_data_service,
        connection_health_monitor=monitor,
        position_tick_service=position_tick_service,
    )

    runner._tick()

    assert sm.state == BotState.SAFE_MODE
    assert sm.can_trade() is False
    # SAFE_MODE sigue gestionando posiciones existentes: el tick de posiciones corre.
    position_tick_service.tick_all.assert_called_once()
    assert heartbeat_file.exists()


def test_tick_with_orphan_order_ends_in_safe_mode(session: Session, heartbeat_file: Path) -> None:
    bot_run = make_bot_run(session, status="RUNNING")
    sm = BotStateMachine(initial=BotState.ACTIVE)

    inner = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter = ChaosAdapter(inner)
    inner.set_leverage("BTCUSDT", 1)
    inner.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=Decimal("10000"),
        )
    )  # PENDING sin fila local → huérfana

    healthy = _snapshots("BTCUSDT", "ETHUSDT")
    market_data_service = Mock(spec=MarketDataCycleService)
    market_data_service.tick_all.return_value = healthy
    monitor = ConnectionHealthMonitor(
        state_machine=sm,
        session=session,
        bot_run_id=bot_run.id,
        max_clock_skew_ms=2000,
        max_latency_ms=3000,
        symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )
    scanner = OrphanOrderScanner(
        adapter=adapter,
        position_manager=PositionManager(adapter),
        state_machine=sm,
        session=session,
        bot_run_id=bot_run.id,
        symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )
    runner = CycleRunner(
        sm,
        interval_seconds=1,
        heartbeat_file=heartbeat_file,
        market_data_service=market_data_service,
        connection_health_monitor=monitor,
        orphan_order_scanner=scanner,
    )

    runner._tick()

    assert sm.state == BotState.SAFE_MODE
    assert sm.can_trade() is False
