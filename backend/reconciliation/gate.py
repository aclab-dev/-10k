"""ReconciliationGate — enforcement de `ReconciliationEngine` sobre el loop real (F16 [159]).

`ReconciliationEngine` (engine.py) solo detecta — su propio docstring lo dice
explícitamente: "no corrige ni muta el adapter ni la DB". La respuesta a los
hallazgos (bloquear nuevas entradas) es responsabilidad de este componente
aparte, en vez de mezclarse con la detección.

Reemplaza a `OrphanOrderScanner` (F16 [115], retirado): su detección (órdenes
vivas sin fila local, posiciones sin `PositionConfig`) era un subconjunto
estricto de la que ya hace `ReconciliationEngine` — correr ambos hacia el
mismo adapter en el mismo tick duplicaba las llamadas al exchange sin agregar
cobertura.

Mapeo discrepancia -> flag de bloqueo (config.yaml `reconciliation:`):
- `block_on_orphan_orders`: OrderDiscrepancy MISSING_IN_DB (orden viva en el
  exchange sin fila local — orden genuinamente ajena/desconocida). NO incluye
  MISSING_IN_ADAPTER (orden PENDING en DB que el exchange ya no reporta viva):
  ese caso significa que la orden se resolvió fuera del bot (fill o
  cancelación) y no hay ningún mecanismo en el sistema que después actualice
  el status local (`ExecutionEngine` persiste la fila una sola vez, al
  colocarla — ver `execute_approved_plan`) — así que toda orden que alguna vez
  se resuelva queda MISSING_IN_ADAPTER *para siempre* en cada tick posterior.
  Bloquear por esto dispararía SAFE_MODE de forma permanente ante el ciclo de
  vida normal de cualquier orden, no ante un riesgo real (revisión de Rodrigo,
  PR #128) — mismo criterio de asimetría que ya aplica del lado de posiciones
  (ver abajo).
- `block_on_untracked_positions`: PositionDiscrepancy MISSING_IN_DB (posición
  abierta en el exchange sin fila OPEN local). NO incluye MISSING_IN_ADAPTER
  (posición OPEN en DB que el exchange ya no reporta) — DB desactualizada no
  es exposición de riesgo no trackeada.
- `block_on_unconfirmed_protection`: PositionDiscrepancy MISSING_PROTECTION.

El resto de los tipos de discrepancia (QUANTITY_MISMATCH, PRICE_MISMATCH,
SIDE_MISMATCH, STATUS_MISMATCH, PARTIAL_FILL, MANUAL_SL_TP_CHANGE, y las dos
variantes MISSING_IN_ADAPTER de arriba) se loguean (quedan en el
`ReconciliationReport` y, si el ciclo sí bloquea por otra razón, en el
`SystemEvent` de auditoría) pero no bloquean por sí solos: la tarjeta que
wireó este componente solo pide estos tres flags.

`failed_symbols`: si `get_position`/`get_open_orders` falla para un símbolo,
`ReconciliationEngine` lo marca como no verificado (`report.is_complete` pasa
a `False`) pero NO agrega ninguna discrepancia por ese símbolo — así que un
reporte incompleto, sin más, no bloquea nuevas entradas acá (mismo criterio de
aislamiento por símbolo que `MarketDataCycleService`: un fallo de transporte
puntual no debe frenar todo el ciclo). Si ese símbolo
tenía además una condición realmente bloqueante, esa sí se evalúa igual sobre
lo que se pudo reconciliar de otros símbolos.

`manual_balance_change_policy` (hoy siempre `UPDATE_ACCOUNT_STATE_ONLY`) no
tiene ningún efecto acá a propósito: `ReconciliationEngine` no compara balance
local vs exchange en su versión actual (no hay ningún `DiscrepancyType` de
balance) — no hay ningún cambio de balance que este gate pudiera revertir.
El valor queda leído desde `config.yaml` para cuando exista esa detección,
sin que este wiring necesite tocarse de nuevo.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.core.config import ReconciliationConfig
from backend.reconciliation.engine import (
    DiscrepancyType,
    ReconciliationEngine,
    ReconciliationReport,
)
from backend.storage.models import SystemEvent
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

_log = structlog.get_logger(__name__)

# Tipo de OrderDiscrepancy que cuenta como "orden huérfana" para
# block_on_orphan_orders (ver docstring del módulo — MISSING_IN_ADAPTER queda
# deliberadamente afuera).
_ORPHAN_ORDER_TYPES = frozenset({DiscrepancyType.MISSING_IN_DB})


class ReconciliationGate:
    """Corre `ReconciliationEngine` en cada tick y aplica SAFE_MODE según los flags de config.

    No thread-safe, mismo criterio que ConnectionHealthMonitor: se asume un
    solo loop llamando run_and_enforce() secuencialmente.
    """

    def __init__(
        self,
        engine: ReconciliationEngine,
        config: ReconciliationConfig,
        state_machine: BotStateMachine,
        session: Session,
        bot_run_id: str,
    ) -> None:
        self._engine = engine
        self._config = config
        self._state_machine = state_machine
        self._session = session
        self._bot_run_id = bot_run_id

    def run_and_enforce(self) -> ReconciliationReport | None:
        """Reconcilia y, si hay hallazgos bloqueantes y el bot está ACTIVE, dispara SAFE_MODE.

        Retorna `None` sin reconciliar nada si `enabled` o `run_before_new_entries`
        están en `False` en config — este gate es específicamente el "antes de
        nuevas entradas"; correr la reconciliación bajo demanda por otra vía
        (script, endpoint) es un caso de uso distinto, ya documentado como gap
        separado en docs/runbook_server.md.

        Un reporte con `failed_symbols` (fetch fallido contra el exchange para
        algún símbolo) no bloquea por sí solo — solo lo hacen las 3 condiciones
        de `_blocking_reasons`, evaluadas sobre lo que sí se pudo reconciliar.
        """
        if not (self._config.enabled and self._config.run_before_new_entries):
            return None

        report = self._engine.reconcile(self._bot_run_id)
        reasons = self._blocking_reasons(report)
        if not reasons:
            _log.info(
                "reconciliation_gate.cycle_clean",
                bot_run_id=self._bot_run_id,
                is_complete=report.is_complete,
                total_discrepancies=report.total_discrepancies,
            )
            return report

        if self._state_machine.state != BotState.ACTIVE:
            _log.warning(
                "reconciliation_gate.findings_while_not_active",
                state=self._state_machine.state.value,
                reasons=reasons,
            )
            return report

        try:
            self._trigger_safe_mode(reasons, report)
        except SQLAlchemyError:
            # Fail-open igual que CycleRunner._sync_state_from_db / ConnectionHealthMonitor:
            # un error transitorio de DB no debe tumbar el tick entero.
            _log.error("reconciliation_gate.safe_mode_persist_failed", exc_info=True)
            self._session.rollback()

        return report

    def _blocking_reasons(self, report: ReconciliationReport) -> list[str]:
        reasons: list[str] = []

        if self._config.block_on_orphan_orders:
            orphan = [
                d for d in report.order_discrepancies if d.discrepancy_type in _ORPHAN_ORDER_TYPES
            ]
            if orphan:
                reasons.append(f"{len(orphan)} orden(es) huérfana(s)")

        if self._config.block_on_untracked_positions:
            untracked = [
                d
                for d in report.position_discrepancies
                if d.discrepancy_type == DiscrepancyType.MISSING_IN_DB
            ]
            if untracked:
                reasons.append(f"{len(untracked)} posición(es) no trackeada(s) en DB")

        if self._config.block_on_unconfirmed_protection:
            unconfirmed = [
                d
                for d in report.position_discrepancies
                if d.discrepancy_type == DiscrepancyType.MISSING_PROTECTION
            ]
            if unconfirmed:
                reasons.append(f"{len(unconfirmed)} posición(es) sin protección confirmada")

        return reasons

    def _trigger_safe_mode(self, reasons: list[str], report: ReconciliationReport) -> None:
        reason = "Reconciliation bloqueó nuevas entradas: " + "; ".join(reasons)

        def _audit_event(bot_run_id: str, now: datetime, _previous: BotState) -> SystemEvent:
            return SystemEvent(
                bot_run_id=bot_run_id,
                timestamp=now,
                event_type="RECONCILIATION_BLOCKED",
                severity="WARNING",
                message=reason,
                details={
                    "reasons": reasons,
                    "position_discrepancies": [
                        d.model_dump(mode="json") for d in report.position_discrepancies
                    ],
                    "order_discrepancies": [
                        d.model_dump(mode="json") for d in report.order_discrepancies
                    ],
                    "failed_symbols": report.failed_symbols,
                },
            )

        try:
            EmergencyStopService(self._session).trigger(
                bot_run_id=self._bot_run_id,
                target=BotState.SAFE_MODE,
                reason=reason,
                audit_event_factory=_audit_event,
                require_current_in=frozenset({BotState.ACTIVE}),
                state_machine=self._state_machine,
                resync_reason="reconciliation_gate_resync",
            )
        except BotRunNotRunningError as exc:
            _log.warning(
                "reconciliation_gate.bot_run_not_running",
                bot_run_id=exc.bot_run_id,
                status=exc.status,
            )
        except UnknownPersistedStateError as exc:
            # Mismo criterio que ConnectionHealthMonitor._trigger_safe_mode: dato
            # corrupto en bot_state, no hay transicion segura que fabricar acá.
            _log.error(
                "reconciliation_gate.unknown_persisted_state",
                bot_run_id=exc.bot_run_id,
                state=exc.raw_state,
            )
        except CurrentStateNotAllowedError as exc:
            _log.warning("reconciliation_gate.state_changed_before_lock", state=exc.current.value)
        except InvalidStateTransitionError as exc:
            _log.error("reconciliation_gate.invalid_transition", current=exc.current.value)
        else:
            _log.warning(
                "reconciliation_gate.safe_mode_triggered",
                reason=reason,
                reasons=reasons,
            )


__all__ = ["ReconciliationGate"]
