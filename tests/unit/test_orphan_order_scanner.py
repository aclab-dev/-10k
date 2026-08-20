"""Tests del OrphanOrderScanner (F16 [115])."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.orphan_order_scanner import OrphanOrderScanner, OrphanReason
from backend.position_manager import PositionConfig, PositionManager
from backend.storage.models import BotState as BotStateRow
from backend.storage.models import SystemEvent
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from tests.unit.conftest import make_bot_run, make_bot_state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_long(adapter: PaperAdapter, symbol: str, qty: Decimal, price: Decimal) -> None:
    adapter.set_leverage(symbol, 1)
    adapter.place_order(
        OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=qty,
            price=price,
        )
    )


def _place_pending_limit(adapter: PaperAdapter, symbol: str, qty: Decimal, price: Decimal) -> None:
    """LIMIT muy por debajo del mercado: queda PENDING sin autofill (ver PaperAdapter)."""
    adapter.set_leverage(symbol, 1)
    adapter.place_order(
        OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=price,
        )
    )


class _FlakyPositionAdapter(PaperAdapter):
    """get_position lanza para un símbolo puntual — simula un error de transporte."""

    def __init__(self, *args, fail_symbol: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._fail_symbol = fail_symbol

    def get_position(self, symbol: str):  # type: ignore[override]
        if symbol == self._fail_symbol:
            raise TimeoutError("simulated network hang")
        return super().get_position(symbol)


def _scanner(
    adapter: PaperAdapter,
    pm: PositionManager,
    session: Session,
    bot_run_id: str,
    *,
    state_machine: BotStateMachine | None = None,
    symbols: frozenset[str] | None = None,
) -> OrphanOrderScanner:
    return OrphanOrderScanner(
        adapter=adapter,
        position_manager=pm,
        state_machine=state_machine or BotStateMachine(initial=BotState.ACTIVE),
        session=session,
        bot_run_id=bot_run_id,
        symbols=symbols or frozenset({"BTCUSDT", "ETHUSDT"}),
    )


# ---------------------------------------------------------------------------
# scan_all — deteccion
# ---------------------------------------------------------------------------


class TestScanAll:
    def test_no_findings_when_reconciled(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("1")))
        scanner = _scanner(adapter, pm, session, bot_run.id)

        assert scanner.scan_all() == []

    def test_detects_unexplained_order_without_position(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _place_pending_limit(adapter, "BTCUSDT", Decimal("1"), Decimal("1"))
        pm = PositionManager(adapter)
        scanner = _scanner(adapter, pm, session, bot_run.id)

        findings = scanner.scan_all()

        assert len(findings) == 1
        assert findings[0].symbol == "BTCUSDT"
        assert findings[0].reason == OrphanReason.UNEXPLAINED_ORDER

    def test_detects_unprotected_position_without_config(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        # Sin pm.set_config: posicion abierta pero no vigilada.
        scanner = _scanner(adapter, pm, session, bot_run.id)

        findings = scanner.scan_all()

        assert len(findings) == 1
        assert findings[0].symbol == "BTCUSDT"
        assert findings[0].reason == OrphanReason.UNPROTECTED_POSITION

    def test_both_reasons_can_fire_for_different_symbols(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))  # sin config
        _place_pending_limit(adapter, "ETHUSDT", Decimal("1"), Decimal("1"))  # sin posicion
        pm = PositionManager(adapter)
        scanner = _scanner(adapter, pm, session, bot_run.id)

        findings = scanner.scan_all()

        by_symbol = {f.symbol: f.reason for f in findings}
        assert by_symbol == {
            "BTCUSDT": OrphanReason.UNPROTECTED_POSITION,
            "ETHUSDT": OrphanReason.UNEXPLAINED_ORDER,
        }

    def test_one_symbol_failing_does_not_block_the_others(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        adapter = _FlakyPositionAdapter(initial_balance_usdt=Decimal("5000"), fail_symbol="BTCUSDT")
        _open_long(adapter, "ETHUSDT", Decimal("1"), Decimal("3000"))
        pm = PositionManager(adapter)
        # ETHUSDT sin config -> deberia detectarse igual pese a que BTCUSDT explota.
        scanner = _scanner(adapter, pm, session, bot_run.id)

        findings = scanner.scan_all()

        assert len(findings) == 1
        assert findings[0].symbol == "ETHUSDT"
        assert findings[0].reason == OrphanReason.UNPROTECTED_POSITION

    def test_all_symbols_failing_returns_empty_without_raising(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        adapter = _FlakyPositionAdapter(initial_balance_usdt=Decimal("5000"), fail_symbol="BTCUSDT")
        pm = PositionManager(adapter)
        scanner = _scanner(adapter, pm, session, bot_run.id, symbols=frozenset({"BTCUSDT"}))

        assert scanner.scan_all() == []


# ---------------------------------------------------------------------------
# scan_and_enforce — SAFE_MODE
# ---------------------------------------------------------------------------


class TestScanAndEnforce:
    def test_triggers_safe_mode_when_active_and_findings(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        sm = BotStateMachine(initial=BotState.ACTIVE)
        scanner = _scanner(adapter, pm, session, bot_run.id, state_machine=sm)

        findings = scanner.scan_and_enforce()

        assert len(findings) == 1
        assert sm.state == BotState.SAFE_MODE

        stored = session.scalars(
            select(BotStateRow).where(BotStateRow.bot_run_id == bot_run.id)
        ).all()
        assert len(stored) == 1
        assert stored[0].state == "SAFE_MODE"
        assert stored[0].previous_state == "ACTIVE"

        events = session.scalars(
            select(SystemEvent).where(SystemEvent.bot_run_id == bot_run.id)
        ).all()
        assert len(events) == 1
        assert events[0].event_type == "ORPHAN_ORDER_DETECTED"
        assert events[0].severity == "WARNING"

    def test_no_action_when_no_findings(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("1")))
        sm = BotStateMachine(initial=BotState.ACTIVE)
        scanner = _scanner(adapter, pm, session, bot_run.id, state_machine=sm)

        findings = scanner.scan_and_enforce()

        assert findings == []
        assert sm.state == BotState.ACTIVE
        assert session.scalars(select(BotStateRow)).first() is None

    def test_does_not_retrigger_when_already_safe_mode(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="SAFE_MODE", previous_state="ACTIVE")
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        sm = BotStateMachine(initial=BotState.SAFE_MODE)
        scanner = _scanner(adapter, pm, session, bot_run.id, state_machine=sm)

        findings = scanner.scan_and_enforce()

        assert len(findings) == 1  # sigue detectando, pero no re-dispara
        assert sm.state == BotState.SAFE_MODE
        # La unica fila de bot_state es la que ya existia antes del scan.
        stored = session.scalars(
            select(BotStateRow).where(BotStateRow.bot_run_id == bot_run.id)
        ).all()
        assert len(stored) == 1
        assert session.scalars(select(SystemEvent)).first() is None

    def test_no_action_when_bot_run_not_running(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="STOPPED")
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        sm = BotStateMachine(initial=BotState.ACTIVE)
        scanner = _scanner(adapter, pm, session, bot_run.id, state_machine=sm)

        findings = scanner.scan_and_enforce()

        assert len(findings) == 1
        # No se persistio nada: el bot_run no estaba RUNNING.
        assert session.scalars(select(BotStateRow)).first() is None
        assert session.scalars(select(SystemEvent)).first() is None
