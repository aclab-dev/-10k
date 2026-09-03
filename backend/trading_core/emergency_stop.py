"""Servicio compartido para disparar una transición de emergencia del bot.

Los tres disparadores de emergency-stop del sistema (kill switch manual —
routes_kill_switch.py—, OrphanOrderScanner y ConnectionHealthMonitor)
reimplementaban cada uno, por separado, la misma secuencia: lock de fila
`FOR UPDATE` sobre `BotRun`, re-lectura del estado persistido, validación de
la transición contra la state machine, persistencia atómica de `BotState` +
evento de auditoría, commit. Este módulo la centraliza.

Lo que NO centraliza, a propósito, porque difiere legítimamente entre
callers:
- Cómo reaccionar a un rechazo (routes_kill_switch.py responde HTTP;
  OrphanOrderScanner/ConnectionHealthMonitor loguean y siguen el loop del
  worker). `trigger()` señaliza cada rechazo con una excepción distinta y
  deja la reacción al caller.
- Qué fila de auditoría persistir (`KillSwitchEvent` vs `SystemEvent`, con
  campos propios de cada uno). El caller la arma vía `audit_event_factory`.
- La precondición de negocio sobre el estado de origen: el kill switch no
  tiene ninguna (confía en la tabla de transiciones); los scanners exigen
  `current == ACTIVE` (`require_current_in`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from backend.storage.models import BotRun, KillSwitchEvent, SystemEvent
from backend.storage.models import BotState as BotStateRow
from backend.storage.repositories.bot import BotStateRepository
from backend.trading_core.bot_state_machine import (
    BotState,
    BotStateMachine,
    InvalidStateTransitionError,
    resolve_persisted_state,
)

AuditEventFactory = Callable[[str, datetime, BotState], KillSwitchEvent | SystemEvent]


class EmergencyStopRejected(Exception):
    """Base de los rechazos de `trigger()`.

    El lock ya fue liberado (rollback) salvo que se haya pasado
    `release_lock_on_reject=False` — ver su docstring en `trigger()`.
    """


class BotRunNotRunningError(EmergencyStopRejected):
    def __init__(self, bot_run_id: str, status: str | None) -> None:
        super().__init__(f"bot_run '{bot_run_id}' no esta RUNNING (status actual: '{status}')")
        self.bot_run_id = bot_run_id
        self.status = status


class UnknownPersistedStateError(EmergencyStopRejected):
    def __init__(self, bot_run_id: str, raw_state: str | None) -> None:
        super().__init__(f"Estado de bot desconocido en base: '{raw_state}'")
        self.bot_run_id = bot_run_id
        self.raw_state = raw_state


class CurrentStateNotAllowedError(EmergencyStopRejected):
    def __init__(self, current: BotState, required: frozenset[BotState]) -> None:
        super().__init__(
            f"Estado actual '{current.value}' no habilita esta transicion "
            f"(se requiere uno de {sorted(s.value for s in required)})"
        )
        self.current = current
        self.required = required


@dataclass(frozen=True)
class EmergencyStopResult:
    bot_state: BotStateRow
    previous_state: BotState
    triggered_at: datetime


class EmergencyStopService:
    """Encapsula lock + resync + validación + persistencia dual + commit."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def trigger(
        self,
        *,
        bot_run_id: str,
        target: BotState,
        reason: str,
        audit_event_factory: AuditEventFactory,
        require_current_in: frozenset[BotState] | None = None,
        state_machine: BotStateMachine | None = None,
        resync_reason: str = "emergency_stop_resync",
        release_lock_on_reject: bool = True,
    ) -> EmergencyStopResult:
        """Dispara la transición a `target`, o rechaza.

        Si se pasa `state_machine` (la copia en memoria de un caller de larga
        vida, ej. el worker), se resincroniza contra el estado persistido
        ANTES de validar, y solo se fuerza a `target` DESPUES de un commit
        exitoso — nunca antes, para no divergir de la DB si el commit falla.

        `release_lock_on_reject` controla si un rechazo hace rollback (libera
        el lock FOR UPDATE) antes de propagar la excepción. Default True: un
        caller de sesión larga (worker) DEBE soltarlo, o el lock queda abierto
        hasta el próximo commit en esa misma sesión (puede bloquear
        indefinidamente a otro trigger sobre el mismo BotRun). Un caller de
        sesión por-request (ej. un endpoint FastAPI con `get_db`) puede pasar
        False: el framework ya hace rollback+close al final del request al
        dejar propagar la excepción, y hacerlo acá antes solo expiraría
        objetos ya cargados en la sesión sin necesidad.
        """
        # populate_existing=True: sin esto, si `bot_run_id` ya esta en la identity
        # map de esta sesion (ej. el kill switch, que llega con el BotRun ya
        # cargado por get_current_bot_run), with_for_update toma el lock a nivel
        # DB pero session.get() devuelve el objeto cacheado con sus atributos
        # viejos — el chequeo de status de abajo podria evaluar un valor leido
        # antes del lock. Verificado empiricamente: sin populate_existing, un
        # UPDATE committeado por otra sesion no se refleja pese al with_for_update.
        bot_run = self._session.get(
            BotRun, bot_run_id, with_for_update=True, populate_existing=True
        )
        # Capturar el valor ANTES de rollback(), no despues: rollback() expira
        # los objetos cargados en la sesion, y una fila que solo existia flush-eada
        # (sin commit) dentro de esta misma transaccion desaparece al hacer
        # rollback — releerla ahi dispara un refresh que revienta con
        # ObjectDeletedError (o, para una fila si comiteada, un autobegin nuevo
        # que vuelve a dejar la sesion "en transaccion", justo lo que el lock
        # release de abajo quiere evitar).
        status = bot_run.status if bot_run is not None else None
        if bot_run is None or status != "RUNNING":
            if release_lock_on_reject:
                self._session.rollback()
            raise BotRunNotRunningError(bot_run_id, status)

        latest = BotStateRepository(self._session).get_latest(bot_run_id)
        raw_state = latest.state if latest is not None else None
        current = resolve_persisted_state(raw_state)
        if current is None:
            if release_lock_on_reject:
                self._session.rollback()
            raise UnknownPersistedStateError(bot_run_id, raw_state)

        if state_machine is not None and current != state_machine.state:
            state_machine.force_set(current, reason=resync_reason)

        if require_current_in is not None and current not in require_current_in:
            if release_lock_on_reject:
                self._session.rollback()
            raise CurrentStateNotAllowedError(current, require_current_in)

        try:
            BotStateMachine(initial=current).transition_to(target, reason=reason)
        except InvalidStateTransitionError:
            if release_lock_on_reject:
                self._session.rollback()
            raise

        now = datetime.now(UTC)
        new_state = BotStateRepository(self._session).save(
            BotStateRow(
                bot_run_id=bot_run_id,
                state=target.value,
                previous_state=current.value,
                reason=reason,
                created_at=now,
            )
        )
        self._session.add(audit_event_factory(bot_run_id, now, current))
        self._session.flush()
        self._session.commit()

        if state_machine is not None:
            state_machine.force_set(target, reason=reason)

        return EmergencyStopResult(bot_state=new_state, previous_state=current, triggered_at=now)


__all__ = [
    "BotRunNotRunningError",
    "CurrentStateNotAllowedError",
    "EmergencyStopRejected",
    "EmergencyStopResult",
    "EmergencyStopService",
    "UnknownPersistedStateError",
]
