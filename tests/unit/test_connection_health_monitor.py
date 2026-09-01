"""Tests del ConnectionHealthMonitor (F16 [117])."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.connection_health import ConnectionAnomalyReason, ConnectionHealthMonitor
from backend.core.config import Environment
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)
from backend.storage.models import BotState as BotStateRow
from backend.storage.models import SystemEvent
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from tests.unit.conftest import make_bot_run, make_bot_state

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


def _candles() -> Candles:
    return Candles(tf_5m=_candle(), tf_15m=_candle(), tf_1h=_candle(), tf_4h=_candle())


def _now() -> datetime:
    return datetime.now(UTC)


def _snapshot(
    *, symbol: str = "BTCUSDT", clock_skew_ms: int = 10, latency_ms: int = 50
) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp_utc=_now(),
        exchange=Exchange.BINGX,
        environment=Environment.PAPER,
        symbol=symbol,
        last_price=Decimal("50005"),
        bid=Decimal("50000"),
        ask=Decimal("50010"),
        spread_absolute=Decimal("10"),
        spread_percent=Decimal("0.02"),
        candles=_candles(),
        volume=Decimal("1000"),
        account_balance_usdt=Decimal("500"),
        open_positions_count=0,
        active_orders_count=0,
        latency_ms=latency_ms,
        exchange_server_time=_now(),
        local_time=_now(),
        clock_skew_ms=clock_skew_ms,
        data_freshness_status=DataFreshnessStatus.FRESH,
        coherence_status=CoherenceStatus.OK,
    )


def _monitor(
    session: Session,
    bot_run_id: str,
    *,
    state_machine: BotStateMachine | None = None,
    symbols: frozenset[str] | None = None,
    max_clock_skew_ms: int = 2000,
    max_latency_ms: int = 3000,
) -> ConnectionHealthMonitor:
    return ConnectionHealthMonitor(
        state_machine=state_machine or BotStateMachine(initial=BotState.ACTIVE),
        session=session,
        bot_run_id=bot_run_id,
        max_clock_skew_ms=max_clock_skew_ms,
        max_latency_ms=max_latency_ms,
        symbols=symbols or frozenset({"BTCUSDT", "ETHUSDT"}),
    )


# ---------------------------------------------------------------------------
# check_all — deteccion pura
# ---------------------------------------------------------------------------


class TestCheckAll:
    def test_no_findings_when_all_symbols_clean(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        monitor = _monitor(session, bot_run.id)

        findings = monitor.check_all([_snapshot(symbol="BTCUSDT"), _snapshot(symbol="ETHUSDT")])

        assert findings == []

    def test_missing_symbol_flagged_as_data_unavailable(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        monitor = _monitor(session, bot_run.id)

        findings = monitor.check_all([_snapshot(symbol="BTCUSDT")])

        assert len(findings) == 1
        assert findings[0].symbol == "ETHUSDT"
        assert findings[0].reason == ConnectionAnomalyReason.SYMBOL_DATA_UNAVAILABLE

    def test_clock_skew_over_threshold_flagged(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        monitor = _monitor(session, bot_run.id, max_clock_skew_ms=2000)

        findings = monitor.check_all(
            [_snapshot(symbol="BTCUSDT", clock_skew_ms=2500), _snapshot(symbol="ETHUSDT")]
        )

        assert len(findings) == 1
        assert findings[0].symbol == "BTCUSDT"
        assert findings[0].reason == ConnectionAnomalyReason.CLOCK_SKEW_EXCEEDED

    def test_negative_clock_skew_over_threshold_flagged(self, session: Session) -> None:
        """El umbral es sobre |clock_skew_ms|: un reloj atrasado tambien cuenta."""
        bot_run = make_bot_run(session)
        monitor = _monitor(session, bot_run.id, max_clock_skew_ms=2000)

        findings = monitor.check_all(
            [_snapshot(symbol="BTCUSDT", clock_skew_ms=-2500), _snapshot(symbol="ETHUSDT")]
        )

        assert len(findings) == 1
        assert findings[0].symbol == "BTCUSDT"
        assert findings[0].reason == ConnectionAnomalyReason.CLOCK_SKEW_EXCEEDED

    def test_latency_over_threshold_flagged(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        monitor = _monitor(session, bot_run.id, max_latency_ms=3000)

        findings = monitor.check_all(
            [_snapshot(symbol="BTCUSDT", latency_ms=3500), _snapshot(symbol="ETHUSDT")]
        )

        assert len(findings) == 1
        assert findings[0].symbol == "BTCUSDT"
        assert findings[0].reason == ConnectionAnomalyReason.LATENCY_EXCEEDED

    def test_symbol_can_have_both_clock_skew_and_latency_findings(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        monitor = _monitor(session, bot_run.id, max_clock_skew_ms=2000, max_latency_ms=3000)

        findings = monitor.check_all(
            [
                _snapshot(symbol="BTCUSDT", clock_skew_ms=2500, latency_ms=3500),
                _snapshot(symbol="ETHUSDT"),
            ]
        )

        reasons = {f.reason for f in findings if f.symbol == "BTCUSDT"}
        assert reasons == {
            ConnectionAnomalyReason.CLOCK_SKEW_EXCEEDED,
            ConnectionAnomalyReason.LATENCY_EXCEEDED,
        }


# ---------------------------------------------------------------------------
# check_and_enforce — SAFE_MODE
# ---------------------------------------------------------------------------


class TestCheckAndEnforce:
    def test_triggers_safe_mode_when_findings_and_active(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        sm = BotStateMachine(initial=BotState.ACTIVE)
        monitor = _monitor(session, bot_run.id, state_machine=sm)

        monitor.check_and_enforce([_snapshot(symbol="BTCUSDT")])  # ETHUSDT falta

        assert sm.state == BotState.SAFE_MODE
        latest_state = (
            session.query(BotStateRow)
            .filter_by(bot_run_id=bot_run.id)
            .order_by(BotStateRow.created_at.desc())
        ).first()
        assert latest_state is not None
        assert latest_state.state == BotState.SAFE_MODE.value
        assert latest_state.previous_state == BotState.ACTIVE.value

        events = session.query(SystemEvent).filter_by(bot_run_id=bot_run.id).all()
        assert len(events) == 1
        assert events[0].event_type == "CONNECTION_HEALTH_ANOMALY"
        assert events[0].severity == "WARNING"

    def test_no_transition_when_no_findings(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        sm = BotStateMachine(initial=BotState.ACTIVE)
        monitor = _monitor(session, bot_run.id, state_machine=sm)

        monitor.check_and_enforce([_snapshot(symbol="BTCUSDT"), _snapshot(symbol="ETHUSDT")])

        assert sm.state == BotState.ACTIVE
        assert session.query(SystemEvent).count() == 0

    def test_no_duplicate_trigger_when_already_safe_mode(self, session: Session) -> None:
        bot_run = make_bot_run(session)
        make_bot_state(session, bot_run, state=BotState.SAFE_MODE.value, previous_state="ACTIVE")
        sm = BotStateMachine(initial=BotState.SAFE_MODE)
        monitor = _monitor(session, bot_run.id, state_machine=sm)

        monitor.check_and_enforce([_snapshot(symbol="BTCUSDT")])  # ETHUSDT falta

        assert sm.state == BotState.SAFE_MODE
        assert session.query(SystemEvent).count() == 0

    def test_rolls_back_on_sqlalchemy_error(self, session: Session, monkeypatch) -> None:
        bot_run = make_bot_run(session)
        sm = BotStateMachine(initial=BotState.ACTIVE)
        monitor = _monitor(session, bot_run.id, state_machine=sm)

        def _boom(*args: object, **kwargs: object) -> None:
            raise SQLAlchemyError("simulated db failure")

        monkeypatch.setattr(session, "commit", _boom)

        monitor.check_and_enforce([_snapshot(symbol="BTCUSDT")])  # ETHUSDT falta

        # Fail-open: el estado en memoria no debe divergir de la DB si el
        # persist fallo.
        assert sm.state == BotState.ACTIVE

    def test_no_action_when_bot_run_not_running(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="STOPPED")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        monitor = _monitor(session, bot_run.id, state_machine=sm)

        findings = monitor.check_and_enforce([_snapshot(symbol="BTCUSDT")])  # ETHUSDT falta

        assert len(findings) == 1
        # No se persistio nada: el bot_run no estaba RUNNING.
        assert session.scalars(select(BotStateRow)).first() is None
        assert session.scalars(select(SystemEvent)).first() is None

    def test_survives_corrupt_persisted_state_without_raising(self, session: Session) -> None:
        """Regresion: un valor en bot_state.state fuera del enum BotState (dato
        corrupto) no debe propagar ValueError hacia CycleRunner._tick() — ese loop
        no tiene try/except propio, asi que una excepcion sin atrapar acá tumbaria
        el worker entero. Mismo criterio de fail-safe que OrphanOrderScanner
        (adaptado del mismo bug/fix, PR #121): loguear y no disparar."""
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="GARBAGE_STATE", previous_state="ACTIVE")
        # commit explicito: la fila corrupta debe sobrevivir al rollback() que
        # _trigger_safe_mode hace para soltar el lock FOR UPDATE.
        session.commit()
        sm = BotStateMachine(initial=BotState.ACTIVE)
        monitor = _monitor(session, bot_run.id, state_machine=sm)

        findings = monitor.check_and_enforce([_snapshot(symbol="BTCUSDT")])  # no debe lanzar

        assert len(findings) == 1
        assert sm.state == BotState.ACTIVE  # no se toco: nunca se pudo determinar el actual
        stored = session.scalars(
            select(BotStateRow).where(BotStateRow.bot_run_id == bot_run.id)
        ).all()
        assert len(stored) == 1  # solo la fila corrupta preexistente, no se agrego nada
        assert session.scalars(select(SystemEvent)).first() is None

    def test_releases_row_lock_on_every_early_return(self, session: Session) -> None:
        """Regresion (mismo bug/fix que OrphanOrderScanner, PR #121): session.get(
        ..., with_for_update=True) abre una transaccion con lock FOR UPDATE sobre
        BotRun. Si un early-return posterior no hace rollback/commit, esa
        transaccion (y el lock) queda abierta hasta el proximo commit en esta
        misma sesion de larga vida del worker — puede bloquear indefinidamente al
        kill switch manual, que toma el mismo lock. Cubre las 3 ramas de
        early-return post-lock: bot_run no RUNNING, estado corrupto, y estado ya
        no-ACTIVE."""
        # Rama 1: bot_run no RUNNING.
        stopped_run = make_bot_run(session, status="STOPPED")
        session.commit()
        monitor1 = _monitor(session, stopped_run.id)
        monitor1.check_and_enforce([_snapshot(symbol="BTCUSDT")])
        assert session.in_transaction() is False

        # Rama 2 y 3 comparten un unico bot_run RUNNING (el indice unico parcial
        # uq_bot_runs_single_running solo permite uno a la vez).
        running_run = make_bot_run(session, status="RUNNING")
        session.commit()

        # Rama 2: estado corrupto en bot_state.
        make_bot_state(session, running_run, state="GARBAGE_STATE", previous_state="ACTIVE")
        session.commit()
        monitor2 = _monitor(session, running_run.id)
        monitor2.check_and_enforce([_snapshot(symbol="BTCUSDT")])
        assert session.in_transaction() is False

        # Rama 3: estado ya no-ACTIVE (releido de DB, no del cache en memoria).
        make_bot_state(session, running_run, state="KILL_SWITCH_TRIGGERED", previous_state="ACTIVE")
        session.commit()
        # state_machine local queda desactualizado a proposito: fuerza a
        # _trigger_safe_mode a re-leer la DB y descubrir el cambio recien ahi.
        sm3 = BotStateMachine(initial=BotState.ACTIVE)
        monitor3 = _monitor(session, running_run.id, state_machine=sm3)
        monitor3.check_and_enforce([_snapshot(symbol="BTCUSDT")])
        assert session.in_transaction() is False
