"""Endpoint del kill switch manual: detiene el bot vía la state machine.

Persiste la transición (bot_state + kill_switch_events) para que el dashboard
refleje el nuevo estado. El worker corre en un proceso separado con su propia
BotStateMachine en memoria; se entera de este cambio releyendo bot_state al
tope de cada iteración de su loop y antes de cada símbolo del pipeline de
decisión (ver CycleRunner._sync_state_from_db / _run_decision_pipeline), no
en el momento exacto del POST. El peor caso no es el intervalo de ciclo: si
el kill switch se dispara mientras un símbolo ya está en medio de su llamada
a GPT (hasta ~30s + reintentos con backoff), ese símbolo en curso termina de
procesarse antes de que la reconciliación pueda frenarlo — el resync antes
de cada símbolo evita que además se abran posiciones para los símbolos
siguientes del mismo tick.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.dependencies import get_current_bot_run
from backend.storage.database import get_db
from backend.storage.models import BotRun, KillSwitchEvent
from backend.trading_core.bot_state_machine import BotState, InvalidStateTransitionError
from backend.trading_core.emergency_stop import (
    BotRunNotRunningError,
    EmergencyStopService,
    UnknownPersistedStateError,
)

router = APIRouter(prefix="/kill-switch", tags=["kill-switch"])
_log = structlog.get_logger()


class KillSwitchRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1024)


class KillSwitchOut(BaseModel):
    bot_run_id: str
    state: str
    previous_state: str
    reason: str
    triggered_at: datetime


@router.post("", status_code=status.HTTP_200_OK)
def trigger_kill_switch(
    body: KillSwitchRequest,
    bot_run: Annotated[BotRun, Depends(get_current_bot_run)],
    db: Annotated[Session, Depends(get_db)],
) -> KillSwitchOut:
    """Dispara el kill switch manual: transiciona a KILL_SWITCH_TRIGGERED."""

    def _audit_event(bot_run_id: str, now: datetime, previous: BotState) -> KillSwitchEvent:
        return KillSwitchEvent(
            bot_run_id=bot_run_id,
            timestamp=now,
            trigger_reason=body.reason,
            state_before=previous.value,
            action_taken="MANUAL_KILL_SWITCH",
            requires_manual_review=True,
        )

    try:
        result = EmergencyStopService(db).trigger(
            bot_run_id=bot_run.id,
            target=BotState.KILL_SWITCH_TRIGGERED,
            reason=body.reason,
            audit_event_factory=_audit_event,
            # get_db ya hace rollback+close al final del request cuando la
            # excepcion se propaga (ver backend/storage/database.py): no hace
            # falta que el servicio lo haga antes.
            release_lock_on_reject=False,
        )
    except BotRunNotRunningError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No se puede activar el kill switch: el bot run '{exc.bot_run_id}' no esta "
            f"RUNNING (status actual: '{exc.status}')",
        ) from exc
    except UnknownPersistedStateError as exc:
        _log.error("kill_switch.unknown_bot_state", bot_run_id=exc.bot_run_id, state=exc.raw_state)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No se puede activar el kill switch desde el estado '{exc.current.value}'",
        ) from exc

    return KillSwitchOut(
        bot_run_id=bot_run.id,
        state=result.bot_state.state,
        previous_state=result.previous_state.value,
        reason=body.reason,
        triggered_at=result.triggered_at,
    )
