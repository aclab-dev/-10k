"""Caos — desconexiones (F16 [118]).

Corta la conectividad con el exchange / feed de datos y valida que el bot deja
de operar en ACTIVE: entra en SAFE_MODE ante pérdida de datos, aísla el símbolo
inalcanzable sin tumbar el resto, y la reconciliación reporta el hueco en vez de
asumir consistencia.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.connection_health.monitor import ConnectionHealthMonitor
from backend.connection_health.schemas import ConnectionAnomalyReason
from backend.core.config import Environment
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.orphan_order_scanner.scanner import OrphanOrderScanner
from backend.position_manager.manager import PositionManager
from backend.reconciliation.engine import ReconciliationEngine
from backend.storage.models import BotState as BotStateRow
from backend.storage.models import SystemEvent
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from backend.trading_core.cycle_runner import CycleRunner
from tests.chaos.faults import ChaosAdapter, InjectedDisconnectError
from tests.unit.conftest import make_bot_run

pytestmark = pytest.mark.chaos

_BOT_RUN_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle() -> CandleData:
    return CandleData(
        open=Decimal("50000"),
        high=Decimal("50100"),
        low=Decimal("49900"),
        close=Decimal("50010"),
        volume=Decimal("100"),
        n_candles=10,
    )


def _snapshot(symbol: str = "BTCUSDT") -> MarketSnapshot:
    now = datetime.now(UTC)
    return MarketSnapshot(
        timestamp_utc=now,
        exchange=Exchange.BINGX,
        environment=Environment.PAPER,
        symbol=symbol,
        last_price=Decimal("50005"),
        bid=Decimal("50000"),
        ask=Decimal("50010"),
        spread_absolute=Decimal("10"),
        spread_percent=Decimal("0.02"),
        candles=Candles(tf_5m=_candle(), tf_15m=_candle(), tf_1h=_candle(), tf_4h=_candle()),
        volume=Decimal("1000"),
        account_balance_usdt=Decimal("500"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=50,
        exchange_server_time=now,
        local_time=now,
        clock_skew_ms=10,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


def _monitor(session: Session, bot_run_id: str, sm: BotStateMachine) -> ConnectionHealthMonitor:
    return ConnectionHealthMonitor(
        state_machine=sm,
        session=session,
        bot_run_id=bot_run_id,
        max_clock_skew_ms=2000,
        max_latency_ms=3000,
        symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )


# ---------------------------------------------------------------------------
# ConnectionHealthMonitor — pérdida de datos
# ---------------------------------------------------------------------------


def test_total_data_loss_forces_safe_mode(session: Session) -> None:
    """Ningún símbolo llega en el ciclo (exchange caído) → SAFE_MODE, y el bot
    deja de poder abrir posiciones nuevas."""
    bot_run = make_bot_run(session, status="RUNNING")
    sm = BotStateMachine(initial=BotState.ACTIVE)
    monitor = _monitor(session, bot_run.id, sm)

    findings = monitor.check_and_enforce([])

    assert {f.reason for f in findings} == {ConnectionAnomalyReason.SYMBOL_DATA_UNAVAILABLE}
    assert sm.state == BotState.SAFE_MODE
    assert sm.can_trade() is False
    latest = (
        session.query(BotStateRow)
        .filter_by(bot_run_id=bot_run.id)
        .order_by(BotStateRow.created_at.desc())
        .first()
    )
    assert latest is not None and latest.state == BotState.SAFE_MODE.value
    events = session.query(SystemEvent).filter_by(bot_run_id=bot_run.id).all()
    assert len(events) == 1
    assert events[0].event_type == "CONNECTION_HEALTH_ANOMALY"


def test_partial_data_loss_still_forces_safe_mode(session: Session) -> None:
    """Un solo símbolo se cae: sigue siendo motivo de SAFE_MODE (no se opera a
    ciegas sobre el símbolo faltante)."""
    bot_run = make_bot_run(session, status="RUNNING")
    sm = BotStateMachine(initial=BotState.ACTIVE)
    monitor = _monitor(session, bot_run.id, sm)

    findings = monitor.check_and_enforce([_snapshot("BTCUSDT")])  # falta ETHUSDT

    assert [f.symbol for f in findings] == ["ETHUSDT"]
    assert sm.state == BotState.SAFE_MODE


# ---------------------------------------------------------------------------
# ReconciliationEngine — exchange inalcanzable
# ---------------------------------------------------------------------------


def test_reconciliation_reports_incomplete_when_symbol_unreachable() -> None:
    inner = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter = ChaosAdapter(inner)
    adapter.fail("get_position", exc=InjectedDisconnectError("socket reset"), symbol="ETHUSDT")
    adapter.fail("get_open_orders", exc=InjectedDisconnectError("socket reset"), symbol="ETHUSDT")

    position_repo = MagicMock()
    position_repo.list_open.return_value = []
    order_repo = MagicMock()
    order_repo.list_by_status.return_value = []
    order_repo.list_by_client_order_ids.return_value = []

    engine = ReconciliationEngine(
        adapter, position_repo, order_repo, symbols=frozenset({"BTCUSDT", "ETHUSDT"})
    )
    report = engine.reconcile(_BOT_RUN_ID)

    assert report.failed_symbols == ["ETHUSDT"]
    assert report.is_complete is False
    assert report.is_consistent is False


# ---------------------------------------------------------------------------
# OrphanOrderScanner — un símbolo inalcanzable no tapa un hallazgo en otro
# ---------------------------------------------------------------------------


def test_orphan_scanner_isolates_unreachable_symbol_and_still_safes(session: Session) -> None:
    bot_run = make_bot_run(session, status="RUNNING")
    inner = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter = ChaosAdapter(inner)
    adapter.fail("get_position", exc=InjectedDisconnectError("hang"), symbol="ETHUSDT")

    # Orden PENDING desconocida en BTCUSDT (sin fila local) → huérfana.
    inner.set_leverage("BTCUSDT", 1)
    inner.place_order(
        OrderRequest(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("0.01"),
            price=Decimal("10000"),
        )
    )

    sm = BotStateMachine(initial=BotState.ACTIVE)
    scanner = OrphanOrderScanner(
        adapter=adapter,
        position_manager=PositionManager(adapter),
        state_machine=sm,
        session=session,
        bot_run_id=bot_run.id,
        symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )

    findings = scanner.scan_and_enforce()  # no debe lanzar por el símbolo caído

    assert [f.symbol for f in findings] == ["BTCUSDT"]
    assert sm.state == BotState.SAFE_MODE


# ---------------------------------------------------------------------------
# CycleRunner — corte de DB al resincronizar el estado
# ---------------------------------------------------------------------------


def test_state_sync_survives_db_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si la DB está caída al releer bot_state, el loop no muere: hace rollback y
    sigue con el último estado conocido (fail-open, pero registrado)."""
    session = Mock()
    runner = CycleRunner(
        BotStateMachine(initial=BotState.ACTIVE),
        interval_seconds=10,
        session=session,
        bot_run_id="run-1",
    )

    def _boom(_self: object, _bot_run_id: str) -> None:
        raise SQLAlchemyError("db unreachable")

    monkeypatch.setattr("backend.trading_core.cycle_runner.BotStateRepository.get_latest", _boom)

    runner._sync_state_from_db()  # no debe propagar

    session.rollback.assert_called_once()
