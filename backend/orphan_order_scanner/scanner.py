"""OrphanOrderScanner — reconciliación periódica exchange vs. estado local (F16 [115]).

Compara, símbolo por símbolo, lo que el exchange reporta (vía ExchangeAdapter)
contra lo que el bot espera localmente (PositionManager). Dos casos de orfandad:

- UNEXPLAINED_ORDER: hay órdenes activas en el exchange sin una posición que
  las explique.
- UNPROTECTED_POSITION: hay una posición abierta en el exchange pero
  PositionManager no tiene un PositionConfig vigilándola. En este sistema el
  SL/TP nunca se materializa como orden STOP resting en el exchange (se
  monitorea con mark price via PositionManager.tick() y se cierra con MARKET
  al dispararse) — "protección" significa config activo en memoria, no una
  orden. Este caso cubre el riesgo ya documentado en PositionManager: un
  restart del worker pierde todos los PositionConfig en memoria y deja
  posiciones abiertas sin ningún monitoreo, en silencio.

Ante cualquier hallazgo con el bot en ACTIVE, dispara SAFE_MODE replicando el
patrón de routes_kill_switch.py (lock de fila del BotRun, transición validada
por la state machine, persistencia atómica de BotState + SystemEvent).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.exchange_adapters.base import ExchangeAdapter
from backend.market_data.schemas import ALLOWED_SYMBOLS
from backend.orphan_order_scanner.schemas import OrphanFinding, OrphanReason
from backend.position_manager.manager import PositionManager
from backend.storage.models import BotRun, SystemEvent
from backend.storage.models import BotState as BotStateRow
from backend.storage.repositories.audit import SystemEventRepository
from backend.storage.repositories.bot import BotStateRepository
from backend.trading_core.bot_state_machine import (
    BotState,
    BotStateMachine,
    InvalidStateTransitionError,
)

_log = structlog.get_logger(__name__)


class OrphanOrderScanner:
    """Escanea los símbolos permitidos en cada tick y aplica SAFE_MODE si hace falta.

    No thread-safe, mismo criterio que el resto de los componentes tickeados
    del worker (PositionTickService, CycleRunner): se asume un solo loop
    llamando scan_and_enforce() secuencialmente.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        position_manager: PositionManager,
        state_machine: BotStateMachine,
        session: Session,
        bot_run_id: str,
        symbols: frozenset[str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._position_manager = position_manager
        self._state_machine = state_machine
        self._session = session
        self._bot_run_id = bot_run_id
        self._symbols = tuple(sorted(symbols or ALLOWED_SYMBOLS))

    def scan_all(self) -> list[OrphanFinding]:
        """Escanea todos los símbolos configurados. Aísla fallas por símbolo:
        un error de transporte en uno no interrumpe el resto del escaneo.
        """
        findings: list[OrphanFinding] = []
        for symbol in self._symbols:
            try:
                position = self._adapter.get_position(symbol)
                orders = self._adapter.get_open_orders(symbol)
            except Exception:
                _log.error("orphan_order_scanner.symbol_scan_failed", symbol=symbol, exc_info=True)
                continue

            if orders and position is None:
                findings.append(
                    OrphanFinding(
                        symbol=symbol,
                        reason=OrphanReason.UNEXPLAINED_ORDER,
                        detail=(
                            f"{len(orders)} orden(es) activa(s) en el exchange sin posicion abierta"
                        ),
                    )
                )

            if position is not None and self._position_manager.get_config(symbol) is None:
                findings.append(
                    OrphanFinding(
                        symbol=symbol,
                        reason=OrphanReason.UNPROTECTED_POSITION,
                        detail=(
                            f"posicion {position.side.value} qty={position.quantity} abierta "
                            "sin PositionConfig activo en PositionManager"
                        ),
                    )
                )

        return findings

    def scan_and_enforce(self) -> list[OrphanFinding]:
        """Escanea y, si hay hallazgos y el bot está ACTIVE, dispara SAFE_MODE.

        Si ya está en SAFE_MODE (u otro estado no-ACTIVE) mientras la orfandad
        persiste, solo loguea — no reintenta una transición inválida ni
        duplica el SystemEvent en cada tick.
        """
        findings = self.scan_all()
        if not findings:
            _log.info("orphan_order_scanner.cycle_clean", symbols_scanned=len(self._symbols))
            return findings

        if self._state_machine.state != BotState.ACTIVE:
            _log.warning(
                "orphan_order_scanner.findings_while_not_active",
                state=self._state_machine.state.value,
                findings=[f.model_dump(mode="json") for f in findings],
            )
            return findings

        try:
            self._trigger_safe_mode(findings)
        except SQLAlchemyError:
            # Fail-open igual que CycleRunner._sync_state_from_db: un error transitorio
            # de DB no debe tumbar el tick entero. El proximo ciclo vuelve a intentarlo.
            _log.error("orphan_order_scanner.safe_mode_persist_failed", exc_info=True)
            self._session.rollback()

        return findings

    def _trigger_safe_mode(self, findings: list[OrphanFinding]) -> None:
        # Lock de fila: evita una carrera con un kill switch manual disparado desde
        # la API en el mismo instante (mismo criterio que routes_kill_switch.py).
        bot_run = self._session.get(BotRun, self._bot_run_id, with_for_update=True)
        if bot_run is None or bot_run.status != "RUNNING":
            _log.warning(
                "orphan_order_scanner.bot_run_not_running",
                bot_run_id=self._bot_run_id,
                status=bot_run.status if bot_run else None,
            )
            return

        latest = BotStateRepository(self._session).get_latest(self._bot_run_id)
        current = BotState(latest.state) if latest is not None else BotState.ACTIVE
        if current != self._state_machine.state:
            self._state_machine.force_set(current, reason="orphan_scanner_resync")
        if current != BotState.ACTIVE:
            # El estado cambio (ej. kill switch manual) entre el ultimo _sync_state_from_db
            # del ciclo y este lock. No hay nada que disparar: ya no esta ACTIVE.
            _log.warning("orphan_order_scanner.state_changed_before_lock", state=current.value)
            return

        reason = "Ordenes huerfanas detectadas: " + "; ".join(
            f"{f.symbol}:{f.reason.value}" for f in findings
        )
        try:
            self._state_machine.transition_to(BotState.SAFE_MODE, reason=reason)
        except InvalidStateTransitionError:
            _log.error("orphan_order_scanner.invalid_transition", current=current.value)
            return

        now = datetime.now(UTC)
        BotStateRepository(self._session).save(
            BotStateRow(
                bot_run_id=self._bot_run_id,
                state=self._state_machine.state.value,
                previous_state=current.value,
                reason=reason,
                created_at=now,
            )
        )
        SystemEventRepository(self._session).save(
            SystemEvent(
                bot_run_id=self._bot_run_id,
                timestamp=now,
                event_type="ORPHAN_ORDER_DETECTED",
                severity="WARNING",
                message=reason,
                details={"findings": [f.model_dump(mode="json") for f in findings]},
            )
        )
        self._session.commit()
        _log.warning(
            "orphan_order_scanner.safe_mode_triggered",
            reason=reason,
            findings_count=len(findings),
        )


__all__ = ["OrphanOrderScanner"]
