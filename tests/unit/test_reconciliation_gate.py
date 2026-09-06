"""Tests de ReconciliationGate (F16 [159]).

El motor de detección (ReconciliationEngine) ya está cubierto exhaustivamente
en test_reconciliation_engine.py — acá se mockea (`Mock(spec=...)`) para
aislar la lógica propia del gate: gating por config, mapeo discrepancia -> flag
de bloqueo, y el disparo de SAFE_MODE vía EmergencyStopService.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.core.config import ReconciliationConfig
from backend.reconciliation.engine import (
    DiscrepancyType,
    OrderDiscrepancy,
    PositionDiscrepancy,
    ReconciliationEngine,
    ReconciliationReport,
)
from backend.reconciliation.gate import ReconciliationGate
from backend.storage.models import BotState as BotStateRow
from backend.storage.models import SystemEvent
from backend.trading_core.bot_state_machine import BotState, BotStateMachine
from tests.unit.conftest import make_bot_run, make_bot_state

_ALL_FLAGS_ON = ReconciliationConfig(
    enabled=True,
    run_before_new_entries=True,
    block_on_orphan_orders=True,
    block_on_untracked_positions=True,
    block_on_unconfirmed_protection=True,
    manual_balance_change_policy="UPDATE_ACCOUNT_STATE_ONLY",
)


def _clean_report(bot_run_id: str) -> ReconciliationReport:
    return ReconciliationReport(bot_run_id=bot_run_id)


def _incomplete_report(bot_run_id: str) -> ReconciliationReport:
    """Reporte parcial: el fetch al exchange fallo para un simbolo, sin ninguna
    discrepancia detectada en el resto."""
    return ReconciliationReport(bot_run_id=bot_run_id, failed_symbols=["BTCUSDT"])


def _report_with(
    bot_run_id: str,
    *,
    position_discrepancies: list[PositionDiscrepancy] | None = None,
    order_discrepancies: list[OrderDiscrepancy] | None = None,
) -> ReconciliationReport:
    return ReconciliationReport(
        bot_run_id=bot_run_id,
        position_discrepancies=position_discrepancies or [],
        order_discrepancies=order_discrepancies or [],
    )


def _orphan_order_discrepancy(
    discrepancy_type: DiscrepancyType = DiscrepancyType.MISSING_IN_DB,
) -> OrderDiscrepancy:
    return OrderDiscrepancy(
        client_order_id="abc123",
        symbol="BTCUSDT",
        discrepancy_type=discrepancy_type,
        detail="orden viva en el exchange sin fila local",
    )


def _untracked_position_discrepancy() -> PositionDiscrepancy:
    return PositionDiscrepancy(
        symbol="BTCUSDT",
        discrepancy_type=DiscrepancyType.MISSING_IN_DB,
        detail="posicion abierta en el exchange sin fila OPEN en DB",
    )


def _unconfirmed_protection_discrepancy() -> PositionDiscrepancy:
    return PositionDiscrepancy(
        symbol="BTCUSDT",
        discrepancy_type=DiscrepancyType.MISSING_PROTECTION,
        detail="posicion abierta sin PositionConfig activo",
    )


def _non_blocking_discrepancy() -> PositionDiscrepancy:
    return PositionDiscrepancy(
        symbol="BTCUSDT",
        discrepancy_type=DiscrepancyType.QUANTITY_MISMATCH,
        detail="cantidad no coincide",
    )


def _gate(
    session: Session,
    bot_run_id: str,
    report: ReconciliationReport,
    *,
    config: ReconciliationConfig = _ALL_FLAGS_ON,
    state_machine: BotStateMachine | None = None,
) -> ReconciliationGate:
    engine = Mock(spec=ReconciliationEngine)
    engine.reconcile.return_value = report
    return ReconciliationGate(
        engine=engine,
        config=config,
        state_machine=state_machine or BotStateMachine(initial=BotState.ACTIVE),
        session=session,
        bot_run_id=bot_run_id,
    )


class TestGating:
    def test_does_not_reconcile_when_disabled(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        config = _ALL_FLAGS_ON.model_copy(update={"enabled": False})
        gate = _gate(session, bot_run.id, _clean_report(bot_run.id), config=config)

        result = gate.run_and_enforce()

        assert result is None
        gate._engine.reconcile.assert_not_called()  # type: ignore[attr-defined]

    def test_does_not_reconcile_when_run_before_new_entries_is_false(
        self, session: Session
    ) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        config = _ALL_FLAGS_ON.model_copy(update={"run_before_new_entries": False})
        gate = _gate(session, bot_run.id, _clean_report(bot_run.id), config=config)

        result = gate.run_and_enforce()

        assert result is None
        gate._engine.reconcile.assert_not_called()  # type: ignore[attr-defined]

    def test_clean_report_does_not_trigger_safe_mode(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        gate = _gate(session, bot_run.id, _clean_report(bot_run.id), state_machine=sm)

        report = gate.run_and_enforce()

        assert report is not None
        assert report.is_consistent
        assert sm.state == BotState.ACTIVE
        assert session.scalars(select(BotStateRow)).first() is None

    def test_incomplete_report_without_discrepancies_does_not_trigger_safe_mode(
        self, session: Session
    ) -> None:
        """failed_symbols (fetch fallido contra el exchange) por si solo no
        bloquea — solo lo hacen las 3 condiciones de config, evaluadas sobre lo
        que si se pudo reconciliar (revision de Rodrigo, PR #128)."""
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        report = _incomplete_report(bot_run.id)
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        result = gate.run_and_enforce()

        assert result is not None
        assert result.is_complete is False
        assert sm.state == BotState.ACTIVE
        assert session.scalars(select(BotStateRow)).first() is None


class TestBlockingReasons:
    def test_orphan_order_missing_in_db_triggers_safe_mode(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        report = _report_with(bot_run.id, order_discrepancies=[_orphan_order_discrepancy()])
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.SAFE_MODE
        events = session.scalars(select(SystemEvent)).all()
        assert len(events) == 1
        assert events[0].event_type == "RECONCILIATION_BLOCKED"

    def test_order_missing_in_adapter_does_not_trigger_safe_mode(self, session: Session) -> None:
        """Una orden PENDING en DB que el exchange ya no reporta viva significa que
        se resolvio fuera del bot (fill o cancelacion) — nada en el sistema
        actualiza despues el status local (ExecutionEngine persiste la fila una
        sola vez, al colocarla), asi que toda orden que se resuelva quedaria
        MISSING_IN_ADAPTER para siempre en cada tick posterior. Bloquear por
        esto dispararia SAFE_MODE de forma permanente ante el ciclo de vida
        normal de cualquier orden, no ante un riesgo real (revision de Rodrigo,
        PR #128) — misma asimetria que ya aplica del lado de posiciones."""
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        report = _report_with(
            bot_run.id,
            order_discrepancies=[_orphan_order_discrepancy(DiscrepancyType.MISSING_IN_ADAPTER)],
        )
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.ACTIVE
        assert session.scalars(select(SystemEvent)).first() is None

    def test_orphan_orders_respect_disabled_flag(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        config = _ALL_FLAGS_ON.model_copy(update={"block_on_orphan_orders": False})
        report = _report_with(bot_run.id, order_discrepancies=[_orphan_order_discrepancy()])
        gate = _gate(session, bot_run.id, report, config=config, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.ACTIVE

    def test_untracked_position_triggers_safe_mode(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        report = _report_with(
            bot_run.id, position_discrepancies=[_untracked_position_discrepancy()]
        )
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.SAFE_MODE

    def test_untracked_positions_respect_disabled_flag(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        config = _ALL_FLAGS_ON.model_copy(update={"block_on_untracked_positions": False})
        report = _report_with(
            bot_run.id, position_discrepancies=[_untracked_position_discrepancy()]
        )
        gate = _gate(session, bot_run.id, report, config=config, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.ACTIVE

    def test_unconfirmed_protection_triggers_safe_mode(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        report = _report_with(
            bot_run.id, position_discrepancies=[_unconfirmed_protection_discrepancy()]
        )
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.SAFE_MODE

    def test_unconfirmed_protection_respects_disabled_flag(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        config = _ALL_FLAGS_ON.model_copy(update={"block_on_unconfirmed_protection": False})
        report = _report_with(
            bot_run.id, position_discrepancies=[_unconfirmed_protection_discrepancy()]
        )
        gate = _gate(session, bot_run.id, report, config=config, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.ACTIVE

    def test_non_blocking_discrepancy_type_never_triggers_safe_mode(self, session: Session) -> None:
        """QUANTITY_MISMATCH (y el resto de tipos sin flag propio) se detecta y
        queda en el reporte, pero esta tarjeta solo pide bloquear por los 3
        flags de config.yaml — no por cualquier discrepancia."""
        bot_run = make_bot_run(session, status="RUNNING")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        report = _report_with(bot_run.id, position_discrepancies=[_non_blocking_discrepancy()])
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.ACTIVE
        assert session.scalars(select(SystemEvent)).first() is None


class TestStateHandling:
    def test_does_not_retrigger_when_already_safe_mode(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="SAFE_MODE", previous_state="ACTIVE")
        sm = BotStateMachine(initial=BotState.SAFE_MODE)
        report = _report_with(bot_run.id, order_discrepancies=[_orphan_order_discrepancy()])
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        gate.run_and_enforce()

        assert sm.state == BotState.SAFE_MODE
        stored = session.scalars(
            select(BotStateRow).where(BotStateRow.bot_run_id == bot_run.id)
        ).all()
        assert len(stored) == 1  # la fila preexistente, no se agrego otra
        assert session.scalars(select(SystemEvent)).first() is None

    def test_no_action_when_bot_run_not_running(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="STOPPED")
        sm = BotStateMachine(initial=BotState.ACTIVE)
        report = _report_with(bot_run.id, order_discrepancies=[_orphan_order_discrepancy()])
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        gate.run_and_enforce()  # no debe lanzar

        assert session.scalars(select(BotStateRow)).first() is None
        assert session.scalars(select(SystemEvent)).first() is None

    def test_state_machine_stays_active_when_persist_fails(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        session.commit()
        sm = BotStateMachine(initial=BotState.ACTIVE)
        report = _report_with(bot_run.id, order_discrepancies=[_orphan_order_discrepancy()])
        gate = _gate(session, bot_run.id, report, state_machine=sm)

        with patch.object(session, "commit", side_effect=SQLAlchemyError("simulated failure")):
            gate.run_and_enforce()  # no debe lanzar

        assert sm.state == BotState.ACTIVE
        assert session.scalars(select(BotStateRow)).first() is None
        assert session.scalars(select(SystemEvent)).first() is None
