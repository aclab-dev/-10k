"""ReconciliationGate — enforcement de `ReconciliationEngine` sobre el loop real (F16 [159]).

`ReconciliationEngine` (engine.py) solo detecta — su propio docstring lo dice
explícitamente: "no corrige ni muta el adapter ni la DB". La respuesta a los
hallazgos (bloquear nuevas entradas) es responsabilidad de este componente,
mismo criterio que separa `OrphanOrderScanner` (que sí detecta y enforce en
una sola clase) de un motor puro — acá la separación ya vino dada por el
propio ReconciliationEngine, así que el enforcement vive en un wrapper aparte
en vez de mezclarse con la detección.

Mapeo discrepancia -> flag de bloqueo (config.yaml `reconciliation:`):
- `block_on_orphan_orders`: OrderDiscrepancy MISSING_IN_DB (orden viva en el
  exchange sin fila local) o MISSING_IN_ADAPTER (orden PENDING localmente que
  el exchange ya no reporta viva). Ambos casos son "orden en estado
  desconocido" en los términos del spec (§3.9.3: "Si una orden queda en
  estado desconocido, Reconciliation Engine debe resolver antes de permitir
  nuevas entradas") — no solo la huérfana en sentido estricto.
- `block_on_untracked_positions`: PositionDiscrepancy MISSING_IN_DB (posición
  abierta en el exchange sin fila OPEN local).
- `block_on_unconfirmed_protection`: PositionDiscrepancy MISSING_PROTECTION.

El resto de los tipos de discrepancia (QUANTITY_MISMATCH, PRICE_MISMATCH,
SIDE_MISMATCH, STATUS_MISMATCH ajeno a las dos categorías de arriba,
PARTIAL_FILL, MANUAL_SL_TP_CHANGE) se loguean pero no bloquean: la tarjeta
que wireó este componente solo pide estos tres flags.

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

# Tipos de OrderDiscrepancy que cuentan como "orden en estado desconocido"
# para block_on_orphan_orders (ver docstring del módulo).
_ORPHAN_ORDER_TYPES = frozenset({DiscrepancyType.MISSING_IN_DB, DiscrepancyType.MISSING_IN_ADAPTER})


class ReconciliationGate:
    """Corre `ReconciliationEngine` en cada tick y aplica SAFE_MODE según los flags de config.

    No thread-safe, mismo criterio que OrphanOrderScanner/ConnectionHealthMonitor:
    se asume un solo loop llamando run_and_enforce() secuencialmente.
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
            # Fail-open igual que CycleRunner._sync_state_from_db / OrphanOrderScanner:
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
                reasons.append(f"{len(orphan)} orden(es) en estado desconocido/huérfana(s)")

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
            # Mismo criterio que OrphanOrderScanner._trigger_safe_mode: dato
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
