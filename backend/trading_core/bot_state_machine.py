"""State machine del bot.

Define los 5 estados operativos del PDF (seccion 4.8) y las transiciones
permitidas. La logica de cuando transicionar vive afuera (kill switch,
risk engine, dashboard manual); este modulo solo enforza transiciones
validas.
"""

from __future__ import annotations

from enum import StrEnum

import structlog

log = structlog.get_logger(__name__)


class BotState(StrEnum):
    """Estados operativos del bot. Strings para serializar facil a JSON/DB."""

    ACTIVE = "ACTIVE"
    SAFE_MODE = "SAFE_MODE"
    HALTED = "HALTED"
    KILL_SWITCH_TRIGGERED = "KILL_SWITCH_TRIGGERED"
    MANUAL_PAUSED = "MANUAL_PAUSED"


# Transiciones permitidas. Origen -> conjunto de destinos validos.
# Conservador: KILL_SWITCH_TRIGGERED solo puede degradar a HALTED tras
# revision manual (PDF 4.8: requires_manual_review en eventos criticos).
_ALLOWED_TRANSITIONS: dict[BotState, frozenset[BotState]] = {
    BotState.ACTIVE: frozenset(
        {
            BotState.SAFE_MODE,
            BotState.HALTED,
            BotState.KILL_SWITCH_TRIGGERED,
            BotState.MANUAL_PAUSED,
        }
    ),
    BotState.SAFE_MODE: frozenset(
        {
            BotState.ACTIVE,
            BotState.HALTED,
            BotState.KILL_SWITCH_TRIGGERED,
            BotState.MANUAL_PAUSED,
        }
    ),
    BotState.HALTED: frozenset({BotState.ACTIVE, BotState.KILL_SWITCH_TRIGGERED}),
    BotState.KILL_SWITCH_TRIGGERED: frozenset({BotState.HALTED}),
    BotState.MANUAL_PAUSED: frozenset({BotState.ACTIVE, BotState.SAFE_MODE}),
}


class InvalidStateTransitionError(Exception):
    """Se intento una transicion que no esta permitida por la tabla."""

    def __init__(self, current: BotState, target: BotState) -> None:
        super().__init__(f"Invalid transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


class BotStateMachine:
    """Maquina de estados del bot. Empieza en ACTIVE por defecto.

    No thread-safe: el caller debe sincronizar si hay multiples threads.
    Por diseno el worker corre en un solo proceso/loop, asi que no aplica.
    """

    def __init__(self, initial: BotState = BotState.ACTIVE) -> None:
        self._state = initial
        log.info("bot_state_machine.init", state=initial.value)

    @property
    def state(self) -> BotState:
        return self._state

    def can_transition_to(self, target: BotState) -> bool:
        return target in _ALLOWED_TRANSITIONS[self._state]

    def transition_to(self, target: BotState, reason: str = "") -> None:
        """Cambia el estado o lanza InvalidStateTransitionError."""
        if not self.can_transition_to(target):
            log.warning(
                "bot_state_machine.invalid_transition",
                current=self._state.value,
                target=target.value,
                reason=reason,
            )
            raise InvalidStateTransitionError(self._state, target)
        previous = self._state
        self._state = target
        log.info(
            "bot_state_machine.transition",
            previous=previous.value,
            current=target.value,
            reason=reason,
        )

    def force_set(self, target: BotState, reason: str = "") -> None:
        """Fuerza el estado sin validar la tabla de transiciones.

        Para reconciliar con un estado persistido por otro proceso (el
        CycleRunner releyendo bot_state, escrito por el endpoint de kill
        switch en el proceso de la API). La transicion ya fue validada por
        quien la persistio; esto solo la refleja localmente.

        Implica un contrato fuerte: la DB es la unica fuente de verdad del
        estado del worker, y toda transicion que quiera sobrevivir debe
        persistirse en bot_state. Quien mueva esta state machine en memoria
        sin escribir esa fila (p.ej. un futuro caller del risk engine) va a
        ver su cambio revertido en la proxima llamada a
        CycleRunner._sync_state_from_db, sin error ni warning.
        """
        if target == self._state:
            return
        previous = self._state
        self._state = target
        log.info(
            "bot_state_machine.force_set",
            previous=previous.value,
            current=target.value,
            reason=reason,
        )

    def can_trade(self) -> bool:
        """Solo ACTIVE permite abrir nuevas posiciones (PDF 4.8)."""
        return self._state == BotState.ACTIVE

    def can_manage_positions(self) -> bool:
        """ACTIVE y SAFE_MODE permiten administrar posiciones existentes."""
        return self._state in {BotState.ACTIVE, BotState.SAFE_MODE}

    def is_running(self) -> bool:
        """HALTED y KILL_SWITCH_TRIGGERED detienen el ciclo. El resto continua."""
        return self._state not in {BotState.HALTED, BotState.KILL_SWITCH_TRIGGERED}


def resolve_persisted_state(raw_state: str | None) -> BotState | None:
    """Mapea el string crudo de la ultima fila `bot_state.state` a BotState.

    Contrato compartido por todo caller que relee estado persistido (hoy
    CycleRunner._sync_state_from_db y OrphanOrderScanner._trigger_safe_mode;
    antes vivia duplicado en los dos):
    - raw_state=None (no hay fila persistida todavia) -> BotState.ACTIVE, el
      default del sistema.
    - raw_state fuera del enum (dato corrupto) -> None. Fail-open explicito:
      no hay forma segura de mapearlo a una transicion valida (mismo criterio
      que routes_kill_switch._current_bot_state, que ante esto responde 500
      en vez de fabricar un estado falso). El caller decide como reaccionar
      -seguir con el ultimo estado en memoria, o abortar la accion en curso-
      pero ninguno debe fabricar un estado falso ni dejar el ValueError
      escapar sin atrapar.
    """
    if raw_state is None:
        return BotState.ACTIVE
    try:
        return BotState(raw_state)
    except ValueError:
        return None
