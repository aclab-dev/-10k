"""Endpoint del kill switch manual: detiene el bot vía la state machine.

Persiste la transición (bot_state + kill_switch_events) para que el dashboard
refleje el nuevo estado. El worker corre en un proceso separado con su propia
BotStateMachine en memoria; se entera de este cambio releyendo bot_state al
tope de cada iteración de su loop (ver CycleRunner._sync_state_from_db), no
en el momento exacto del POST — hay hasta un intervalo de ciclo de latencia.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.dependencies import get_current_bot_run
from backend.storage.database import get_db
from backend.storage.models import BotRun, KillSwitchEvent
from backend.storage.models import BotState as BotStateRow
from backend.storage.repositories import BotStateRepository, KillSwitchEventRepository
from backend.trading_core.bot_state_machine import (
    BotState,
    BotStateMachine,
    InvalidStateTransitionError,
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


def _current_bot_state(db: Session, bot_run_id: str) -> BotState:
    latest = BotStateRepository(db).get_latest(bot_run_id)
    if latest is None:
        return BotState.ACTIVE
    try:
        return BotState(latest.state)
    except ValueError as exc:
        # No hay forma segura de mapear un estado fuera del enum a una
        # transición válida: fabricar ACTIVE escribiría un estado falso en
        # la auditoría (bot_state.previous_state / kill_switch_events.state_before).
        # Falla explícita en vez de silenciar el dato corrupto.
        _log.error("kill_switch.unknown_bot_state", bot_run_id=bot_run_id, state=latest.state)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Estado de bot desconocido en base: '{latest.state}'",
        ) from exc


@router.post("", status_code=status.HTTP_200_OK)
def trigger_kill_switch(
    body: KillSwitchRequest,
    bot_run: Annotated[BotRun, Depends(get_current_bot_run)],
    db: Annotated[Session, Depends(get_db)],
) -> KillSwitchOut:
    """Dispara el kill switch manual: transiciona a KILL_SWITCH_TRIGGERED."""
    # Lock de fila: sin esto, dos POST concurrentes leen el mismo estado
    # actual, ambos pasan la validacion de transicion y quedan dos filas en
    # bot_state/kill_switch_events para el mismo hecho. SQLite (tests) no
    # soporta FOR UPDATE y SQLAlchemy lo omite silenciosamente ahi (el motor
    # ya serializa escrituras); Postgres si lo respeta.
    db.execute(select(BotRun).where(BotRun.id == bot_run.id).with_for_update())

    if bot_run.status != "RUNNING":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No se puede activar el kill switch: el bot run '{bot_run.id}' no esta "
            f"RUNNING (status actual: '{bot_run.status}')",
        )

    current = _current_bot_state(db, bot_run.id)
    machine = BotStateMachine(initial=current)

    try:
        machine.transition_to(BotState.KILL_SWITCH_TRIGGERED, reason=body.reason)
    except InvalidStateTransitionError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"No se puede activar el kill switch desde el estado '{current.value}'",
        ) from exc

    now = datetime.now(UTC)

    new_state = BotStateRepository(db).save(
        BotStateRow(
            bot_run_id=bot_run.id,
            state=machine.state.value,
            previous_state=current.value,
            reason=body.reason,
            created_at=now,
        )
    )
    KillSwitchEventRepository(db).save(
        KillSwitchEvent(
            bot_run_id=bot_run.id,
            timestamp=now,
            trigger_reason=body.reason,
            state_before=current.value,
            action_taken="MANUAL_KILL_SWITCH",
            requires_manual_review=True,
        )
    )
    db.commit()

    return KillSwitchOut(
        bot_run_id=bot_run.id,
        state=new_state.state,
        previous_state=current.value,
        reason=body.reason,
        triggered_at=now,
    )
