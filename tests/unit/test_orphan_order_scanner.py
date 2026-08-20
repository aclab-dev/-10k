"""Tests del OrphanOrderScanner (F16 [115])."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.orphan_order_scanner import OrphanOrderScanner, OrphanReason
from backend.position_manager import PositionConfig, PositionManager
from backend.storage.models import BotState as BotStateRow
from backend.storage.models import Order, SystemEvent
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


def _place_pending_limit(adapter: PaperAdapter, symbol: str, qty: Decimal, price: Decimal) -> str:
    """LIMIT muy por debajo del mercado: queda PENDING sin autofill (ver PaperAdapter).

    Devuelve el client_order_id, para que el caller decida si lo persiste como
    fila local `Order` (entrada reconocida) o lo deja sin registrar (huerfana).
    """
    adapter.set_leverage(symbol, 1)
    result = adapter.place_order(
        OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=price,
        )
    )
    return result.client_order_id


def _persist_local_order(
    session: Session, bot_run_id: str, *, client_order_id: str, symbol: str, quantity: Decimal
) -> Order:
    """Simula lo que ExecutionEngine.execute_approved_plan persiste al colocar una
    orden (engine.py linea 313): la fila que hace que el scanner reconozca la
    orden como propia en vez de marcarla huerfana."""
    order = Order(
        bot_run_id=bot_run_id,
        client_order_id=client_order_id,
        symbol=symbol,
        environment="PAPER",
        order_type="LIMIT",
        side="BUY",
        quantity=quantity,
        status="PENDING",
    )
    session.add(order)
    session.flush()
    return order


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

    def test_does_not_flag_a_known_pending_limit_entry(self, session: Session) -> None:
        """Regresion: una entrada LIMIT legitima del propio bot (GPT eligiendo
        EntryType.LIMIT) queda PENDING en el exchange hasta que se llena —
        PaperAdapter no simula fills de non-MARKET. Sin cruzar contra la fila
        local `orders` (lo que persiste ExecutionEngine al colocarla), el scanner
        la marcaria huerfana en el primer tick posterior y dispararia SAFE_MODE
        sobre una entrada normal, no una orfandad real."""
        bot_run = make_bot_run(session)
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        client_order_id = _place_pending_limit(adapter, "BTCUSDT", Decimal("1"), Decimal("1"))
        _persist_local_order(
            session,
            bot_run.id,
            client_order_id=client_order_id,
            symbol="BTCUSDT",
            quantity=Decimal("1"),
        )
        pm = PositionManager(adapter)
        scanner = _scanner(adapter, pm, session, bot_run.id)

        assert scanner.scan_all() == []

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

    def test_survives_corrupt_persisted_state_without_raising(self, session: Session) -> None:
        """Regresion: un valor en bot_state.state fuera del enum BotState (dato
        corrupto) no debe propagar ValueError hacia CycleRunner._tick() — ese
        loop no tiene try/except propio, asi que una excepcion sin atrapar acá
        tumbaria el worker entero. Mismo criterio de fail-safe que
        routes_kill_switch._current_bot_state (que ante esto devuelve 500 en vez
        de fabricar un estado falso), adaptado a que acá no hay HTTP al que
        responder: loguear y no disparar."""
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="GARBAGE_STATE", previous_state="ACTIVE")
        # commit explicito: la fila corrupta debe sobrevivir al rollback() que
        # _trigger_safe_mode hace para soltar el lock FOR UPDATE (fix del review
        # de Rodrigo en el PR #121) — en produccion esa fila ya vendria comprometida
        # de un ciclo anterior, no colgando sin commitear en la misma transaccion.
        session.commit()
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        sm = BotStateMachine(initial=BotState.ACTIVE)
        scanner = _scanner(adapter, pm, session, bot_run.id, state_machine=sm)

        findings = scanner.scan_and_enforce()  # no debe lanzar

        assert len(findings) == 1
        assert sm.state == BotState.ACTIVE  # no se toco: nunca se pudo determinar el actual
        stored = session.scalars(
            select(BotStateRow).where(BotStateRow.bot_run_id == bot_run.id)
        ).all()
        assert len(stored) == 1  # solo la fila corrupta preexistente, no se agrego nada
        assert session.scalars(select(SystemEvent)).first() is None

    def test_releases_row_lock_on_every_early_return(self, session: Session) -> None:
        """Regresion (review PR #121, hallazgo bloqueante): session.get(..., with_for_update=True)
        abre una transaccion con lock FOR UPDATE sobre BotRun. Si un early-return
        posterior no hace rollback/commit, esa transaccion (y el lock) queda abierta
        hasta el proximo commit en esta misma sesion de larga vida del worker — puede
        bloquear indefinidamente al kill switch manual, que toma el mismo lock.
        Cubre las 3 ramas de early-return post-lock: bot_run no RUNNING, estado
        corrupto, y estado ya no-ACTIVE."""
        # Rama 1: bot_run no RUNNING.
        stopped_run = make_bot_run(session, status="STOPPED")
        session.commit()
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        scanner = _scanner(adapter, pm, session, stopped_run.id)
        scanner.scan_and_enforce()
        assert session.in_transaction() is False

        # Rama 2 y 3 comparten un unico bot_run RUNNING (el indice unico parcial
        # uq_bot_runs_single_running solo permite uno a la vez).
        running_run = make_bot_run(session, status="RUNNING")
        session.commit()

        # Rama 2: estado corrupto en bot_state.
        make_bot_state(session, running_run, state="GARBAGE_STATE", previous_state="ACTIVE")
        session.commit()
        scanner2 = _scanner(adapter, pm, session, running_run.id)
        scanner2.scan_and_enforce()
        assert session.in_transaction() is False

        # Rama 3: estado ya no-ACTIVE (releido de DB, no del cache en memoria).
        make_bot_state(session, running_run, state="KILL_SWITCH_TRIGGERED", previous_state="ACTIVE")
        session.commit()
        # state_machine local queda desactualizado a proposito: fuerza a
        # _trigger_safe_mode a re-leer la DB y descubrir el cambio recien ahi.
        sm3 = BotStateMachine(initial=BotState.ACTIVE)
        scanner3 = _scanner(adapter, pm, session, running_run.id, state_machine=sm3)
        scanner3.scan_and_enforce()
        assert session.in_transaction() is False

    def test_state_machine_stays_active_when_persist_fails(self, session: Session) -> None:
        """Regresion (review PR #121, hallazgo bloqueante): antes, self._state_machine
        se mutaba a SAFE_MODE ANTES del commit. Si el commit fallaba (error
        transitorio de DB), la memoria quedaba en SAFE_MODE mientras la DB seguia
        en ACTIVE — el proximo _sync_state_from_db revertia el SAFE_MODE en
        silencio sin que el bot hubiera dejado de operar nunca. Ahora la memoria
        solo se actualiza despues de un commit exitoso."""
        bot_run = make_bot_run(session, status="RUNNING")
        session.commit()
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        sm = BotStateMachine(initial=BotState.ACTIVE)
        scanner = _scanner(adapter, pm, session, bot_run.id, state_machine=sm)

        with patch.object(session, "commit", side_effect=SQLAlchemyError("simulated failure")):
            findings = scanner.scan_and_enforce()  # no debe lanzar

        assert len(findings) == 1
        assert sm.state == BotState.ACTIVE  # nunca se muto: el commit fallo antes
        assert session.scalars(select(BotStateRow)).first() is None
        assert session.scalars(select(SystemEvent)).first() is None
