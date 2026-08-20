"""Tests del BotStateMachine."""

from __future__ import annotations

import pytest

from backend.trading_core.bot_state_machine import (
    BotState,
    BotStateMachine,
    InvalidStateTransitionError,
    resolve_persisted_state,
)


def test_default_initial_state_is_active() -> None:
    sm = BotStateMachine()
    assert sm.state == BotState.ACTIVE


def test_initial_state_can_be_set_explicitly() -> None:
    sm = BotStateMachine(initial=BotState.SAFE_MODE)
    assert sm.state == BotState.SAFE_MODE


@pytest.mark.parametrize(
    ("origin", "target"),
    [
        (BotState.ACTIVE, BotState.SAFE_MODE),
        (BotState.ACTIVE, BotState.HALTED),
        (BotState.ACTIVE, BotState.KILL_SWITCH_TRIGGERED),
        (BotState.ACTIVE, BotState.MANUAL_PAUSED),
        (BotState.SAFE_MODE, BotState.ACTIVE),
        (BotState.SAFE_MODE, BotState.HALTED),
        (BotState.HALTED, BotState.ACTIVE),
        (BotState.HALTED, BotState.KILL_SWITCH_TRIGGERED),
        (BotState.KILL_SWITCH_TRIGGERED, BotState.HALTED),
        (BotState.MANUAL_PAUSED, BotState.ACTIVE),
        (BotState.MANUAL_PAUSED, BotState.SAFE_MODE),
    ],
)
def test_valid_transitions_succeed(origin: BotState, target: BotState) -> None:
    sm = BotStateMachine(initial=origin)
    sm.transition_to(target, reason="test")
    assert sm.state == target


@pytest.mark.parametrize(
    ("origin", "target"),
    [
        # KILL_SWITCH_TRIGGERED solo puede degradar a HALTED, no a otros.
        (BotState.KILL_SWITCH_TRIGGERED, BotState.ACTIVE),
        (BotState.KILL_SWITCH_TRIGGERED, BotState.SAFE_MODE),
        # HALTED no puede saltar a SAFE_MODE o MANUAL_PAUSED, requiere ACTIVE primero.
        (BotState.HALTED, BotState.SAFE_MODE),
        (BotState.HALTED, BotState.MANUAL_PAUSED),
        # MANUAL_PAUSED no puede saltar directo a HALTED ni KILL_SWITCH.
        (BotState.MANUAL_PAUSED, BotState.HALTED),
        (BotState.MANUAL_PAUSED, BotState.KILL_SWITCH_TRIGGERED),
    ],
)
def test_invalid_transitions_raise(origin: BotState, target: BotState) -> None:
    sm = BotStateMachine(initial=origin)
    with pytest.raises(InvalidStateTransitionError):
        sm.transition_to(target)
    # El estado debe quedar igual cuando la transicion falla.
    assert sm.state == origin


def test_can_trade_only_when_active() -> None:
    for state in BotState:
        sm = BotStateMachine(initial=state)
        assert sm.can_trade() is (state == BotState.ACTIVE)


def test_can_manage_positions_in_active_and_safe_mode() -> None:
    expected = {BotState.ACTIVE, BotState.SAFE_MODE}
    for state in BotState:
        sm = BotStateMachine(initial=state)
        assert sm.can_manage_positions() is (state in expected)


def test_is_running_excludes_halted_and_kill_switch() -> None:
    stopped = {BotState.HALTED, BotState.KILL_SWITCH_TRIGGERED}
    for state in BotState:
        sm = BotStateMachine(initial=state)
        assert sm.is_running() is (state not in stopped)


def test_invalid_transition_error_carries_state_info() -> None:
    sm = BotStateMachine(initial=BotState.HALTED)
    with pytest.raises(InvalidStateTransitionError) as exc_info:
        sm.transition_to(BotState.SAFE_MODE)
    assert exc_info.value.current == BotState.HALTED
    assert exc_info.value.target == BotState.SAFE_MODE


class TestResolvePersistedState:
    """Helper compartido por CycleRunner._sync_state_from_db y
    OrphanOrderScanner._trigger_safe_mode (antes duplicado en los dos)."""

    def test_none_raw_state_defaults_to_active(self) -> None:
        assert resolve_persisted_state(None) == BotState.ACTIVE

    @pytest.mark.parametrize("state", list(BotState))
    def test_known_state_maps_to_itself(self, state: BotState) -> None:
        assert resolve_persisted_state(state.value) == state

    def test_unknown_raw_state_returns_none(self) -> None:
        assert resolve_persisted_state("GARBAGE_STATE") is None
