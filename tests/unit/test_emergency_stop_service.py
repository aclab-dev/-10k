"""Tests de EmergencyStopService (F16 [158])."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.storage.models import BotState as BotStateRow
from backend.storage.models import KillSwitchEvent, SystemEvent
from backend.trading_core.bot_state_machine import (
    BotState,
    BotStateMachine,
    InvalidStateTransitionError,
)
from backend.trading_core.emergency_stop import (
    BotRunNotRunningError,
    CurrentStateNotAllowedError,
    EmergencyStopService,
    UnknownPersistedStateError,
)
from tests.unit.conftest import make_bot_run, make_bot_state


def _kill_switch_audit(bot_run_id: str, now: datetime, previous: BotState) -> KillSwitchEvent:
    return KillSwitchEvent(
        bot_run_id=bot_run_id,
        timestamp=now,
        trigger_reason="test",
        state_before=previous.value,
        action_taken="MANUAL_KILL_SWITCH",
        requires_manual_review=True,
    )


def _system_audit(bot_run_id: str, now: datetime, _previous: BotState) -> SystemEvent:
    return SystemEvent(
        bot_run_id=bot_run_id,
        timestamp=now,
        event_type="TEST_EVENT",
        severity="WARNING",
        message="test",
        details=None,
    )


class TestHappyPath:
    def test_ephemeral_caller_without_state_machine(self, session: Session) -> None:
        """Modo kill-switch: sin state_machine persistente."""
        bot_run = make_bot_run(session, status="RUNNING")
        session.commit()

        result = EmergencyStopService(session).trigger(
            bot_run_id=bot_run.id,
            target=BotState.KILL_SWITCH_TRIGGERED,
            reason="operador",
            audit_event_factory=_kill_switch_audit,
        )

        assert result.previous_state == BotState.ACTIVE
        assert result.bot_state.state == "KILL_SWITCH_TRIGGERED"

        stored = session.scalars(
            select(BotStateRow).where(BotStateRow.bot_run_id == bot_run.id)
        ).all()
        assert len(stored) == 1
        assert stored[0].state == "KILL_SWITCH_TRIGGERED"
        assert stored[0].previous_state == "ACTIVE"

        events = session.scalars(
            select(KillSwitchEvent).where(KillSwitchEvent.bot_run_id == bot_run.id)
        ).all()
        assert len(events) == 1
        assert events[0].state_before == "ACTIVE"

    def test_persistent_state_machine_resyncs_and_updates_after_commit(
        self, session: Session
    ) -> None:
        """Modo scanner: state_machine desactualizada se resincroniza y se
        mueve al target solo despues del commit."""
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="ACTIVE")
        session.commit()

        sm = BotStateMachine(initial=BotState.ACTIVE)

        result = EmergencyStopService(session).trigger(
            bot_run_id=bot_run.id,
            target=BotState.SAFE_MODE,
            reason="ordenes huerfanas",
            audit_event_factory=_system_audit,
            require_current_in=frozenset({BotState.ACTIVE}),
            state_machine=sm,
            resync_reason="test_resync",
        )

        assert sm.state == BotState.SAFE_MODE
        assert result.bot_state.state == "SAFE_MODE"

    def test_resyncs_stale_state_machine_before_validating(self, session: Session) -> None:
        """La state_machine en memoria esta desactualizada (dice ACTIVE) pero la
        DB ya tiene SAFE_MODE (ej. otro trigger corrio antes). Debe resincronizar
        contra la DB antes de decidir si la transicion es valida."""
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="SAFE_MODE", previous_state="ACTIVE")
        session.commit()

        sm = BotStateMachine(initial=BotState.ACTIVE)  # desactualizada a proposito

        result = EmergencyStopService(session).trigger(
            bot_run_id=bot_run.id,
            target=BotState.KILL_SWITCH_TRIGGERED,
            reason="test",
            audit_event_factory=_system_audit,
            state_machine=sm,
            resync_reason="test_resync",
        )

        assert result.previous_state == BotState.SAFE_MODE
        assert sm.state == BotState.KILL_SWITCH_TRIGGERED


class TestRejections:
    def test_bot_run_not_running(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="STOPPED")
        session.commit()
        bot_run_id = bot_run.id  # capturado antes: rollback() expira `bot_run`

        try:
            EmergencyStopService(session).trigger(
                bot_run_id=bot_run_id,
                target=BotState.KILL_SWITCH_TRIGGERED,
                reason="test",
                audit_event_factory=_kill_switch_audit,
            )
            raise AssertionError("expected BotRunNotRunningError")
        except BotRunNotRunningError as exc:
            assert exc.bot_run_id == bot_run_id
            assert exc.status == "STOPPED"

        assert session.in_transaction() is False
        assert session.scalars(select(BotStateRow)).first() is None

    def test_bot_run_missing(self, session: Session) -> None:
        try:
            EmergencyStopService(session).trigger(
                bot_run_id="does-not-exist",
                target=BotState.KILL_SWITCH_TRIGGERED,
                reason="test",
                audit_event_factory=_kill_switch_audit,
            )
            raise AssertionError("expected BotRunNotRunningError")
        except BotRunNotRunningError as exc:
            assert exc.status is None

        assert session.in_transaction() is False

    def test_unknown_persisted_state(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="GARBAGE_STATE")
        session.commit()

        try:
            EmergencyStopService(session).trigger(
                bot_run_id=bot_run.id,
                target=BotState.KILL_SWITCH_TRIGGERED,
                reason="test",
                audit_event_factory=_kill_switch_audit,
            )
            raise AssertionError("expected UnknownPersistedStateError")
        except UnknownPersistedStateError as exc:
            assert exc.raw_state == "GARBAGE_STATE"

        assert session.in_transaction() is False
        # La fila corrupta preexistente sigue ahi: no se agrego nada nuevo.
        stored = session.scalars(select(BotStateRow)).all()
        assert len(stored) == 1
        assert stored[0].state == "GARBAGE_STATE"

    def test_current_state_not_allowed(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="MANUAL_PAUSED", previous_state="ACTIVE")
        session.commit()

        sm = BotStateMachine(initial=BotState.MANUAL_PAUSED)

        try:
            EmergencyStopService(session).trigger(
                bot_run_id=bot_run.id,
                target=BotState.SAFE_MODE,
                reason="test",
                audit_event_factory=_system_audit,
                require_current_in=frozenset({BotState.ACTIVE}),
                state_machine=sm,
            )
            raise AssertionError("expected CurrentStateNotAllowedError")
        except CurrentStateNotAllowedError as exc:
            assert exc.current == BotState.MANUAL_PAUSED
            assert exc.required == frozenset({BotState.ACTIVE})

        assert session.in_transaction() is False
        assert sm.state == BotState.MANUAL_PAUSED  # resync ocurrio, pero no el target

    def test_invalid_transition(self, session: Session) -> None:
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="KILL_SWITCH_TRIGGERED", previous_state="ACTIVE")
        session.commit()

        try:
            EmergencyStopService(session).trigger(
                bot_run_id=bot_run.id,
                target=BotState.KILL_SWITCH_TRIGGERED,
                reason="test",
                audit_event_factory=_kill_switch_audit,
            )
            raise AssertionError("expected InvalidStateTransitionError")
        except InvalidStateTransitionError as exc:
            assert exc.current == BotState.KILL_SWITCH_TRIGGERED
            assert exc.target == BotState.KILL_SWITCH_TRIGGERED

        assert session.in_transaction() is False
        stored = session.scalars(select(BotStateRow)).all()
        assert len(stored) == 1  # solo la fila preexistente

    def test_release_lock_on_reject_false_skips_rollback(self, session: Session) -> None:
        """Un caller por-request (release_lock_on_reject=False) no debe hacer
        rollback el mismo: deja que el framework lo haga al final del request.
        Una fila flush-eada-sin-commit antes del rechazo debe seguir visible
        en la sesion."""
        bot_run = make_bot_run(session, status="RUNNING")
        make_bot_state(session, bot_run, state="GARBAGE_STATE")  # solo flush, sin commit

        try:
            EmergencyStopService(session).trigger(
                bot_run_id=bot_run.id,
                target=BotState.KILL_SWITCH_TRIGGERED,
                reason="test",
                audit_event_factory=_kill_switch_audit,
                release_lock_on_reject=False,
            )
            raise AssertionError("expected UnknownPersistedStateError")
        except UnknownPersistedStateError:
            pass

        # Sin rollback propio: la fila flush-eada sigue visible en esta sesion.
        stored = session.scalars(select(BotStateRow).where(BotStateRow.bot_run_id == bot_run.id))
        assert len(stored.all()) == 1


class TestCommitOrdering:
    def test_commit_failure_propagates_without_mutating_state_machine(
        self, session: Session
    ) -> None:
        """Regresion equivalente a la de OrphanOrderScanner (PR #121): si el
        commit falla, la state_machine persistente no debe haberse movido al
        target. El servicio no atrapa el error de commit — es responsabilidad
        del caller (igual que scan_and_enforce/check_and_enforce)."""
        from unittest.mock import patch

        from sqlalchemy.exc import SQLAlchemyError

        bot_run = make_bot_run(session, status="RUNNING")
        session.commit()
        sm = BotStateMachine(initial=BotState.ACTIVE)

        with patch.object(session, "commit", side_effect=SQLAlchemyError("simulated")):
            try:
                EmergencyStopService(session).trigger(
                    bot_run_id=bot_run.id,
                    target=BotState.SAFE_MODE,
                    reason="test",
                    audit_event_factory=_system_audit,
                    require_current_in=frozenset({BotState.ACTIVE}),
                    state_machine=sm,
                )
                raise AssertionError("expected SQLAlchemyError to propagate")
            except SQLAlchemyError:
                pass

        assert sm.state == BotState.ACTIVE
