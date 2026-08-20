"""OrphanOrderScanner — reconciliación periódica exchange vs. estado local (F16 [115]).

Compara, símbolo por símbolo, lo que el exchange reporta (vía ExchangeAdapter)
contra lo que el bot espera localmente (PositionManager + tabla `orders`). Dos
casos de orfandad:

- UNEXPLAINED_ORDER: hay órdenes activas en el exchange sin ninguna fila local
  en `orders` (por client_order_id) que las explique. No alcanza con "sin
  posición abierta": una entrada LIMIT legítima (ExecutionEngine._build_order_request,
  decision.entry_type == EntryType.LIMIT) queda PENDING en el exchange hasta
  que se llena — PaperAdapter no simula el fill de non-MARKET (_register_pending_order) —
  y sin este cruce el scanner la marcaría huérfana en el tick siguiente a
  colocarla, disparando SAFE_MODE sobre una entrada normal del propio bot.
  ExecutionEngine persiste una fila `Order` con `client_order_id=decision.decision_id`
  para toda orden que coloca (engine.py linea 313), así que cualquier orden que
  el bot reconoce tiene esa fila — lo que no la tiene es genuinamente ajeno.
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
por la state machine, persistencia atómica de BotState + SystemEvent). El
estado en memoria se muta recién después de que el commit tiene éxito, no
antes: si la persistencia falla, memoria y DB no deben divergir (ver
_trigger_safe_mode).
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
from backend.storage.repositories.trades import OrderRepository
from backend.trading_core.bot_state_machine import (
    BotState,
    BotStateMachine,
    resolve_persisted_state,
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
        self._order_repo = OrderRepository(session)

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

            # Una sola query por símbolo (IN) en vez de una por orden activa.
            known_ids = self._order_repo.list_known_client_order_ids(
                [o.client_order_id for o in orders]
            )
            unrecognized = [o for o in orders if o.client_order_id not in known_ids]
            if unrecognized:
                findings.append(
                    OrphanFinding(
                        symbol=symbol,
                        reason=OrphanReason.UNEXPLAINED_ORDER,
                        detail=(
                            f"{len(unrecognized)} orden(es) activa(s) en el exchange sin fila "
                            "local en 'orders' (client_order_id desconocido)"
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
        # A partir de acá, TODO return debe soltar la transaccion (rollback): sin
        # eso el lock FOR UPDATE queda abierto hasta el proximo commit en esta
        # misma sesion de larga vida del worker, y puede bloquear indefinidamente
        # al kill switch manual, que toma el mismo lock sobre el mismo BotRun
        # (review PR #121).
        bot_run = self._session.get(BotRun, self._bot_run_id, with_for_update=True)
        if bot_run is None or bot_run.status != "RUNNING":
            _log.warning(
                "orphan_order_scanner.bot_run_not_running",
                bot_run_id=self._bot_run_id,
                status=bot_run.status if bot_run else None,
            )
            self._session.rollback()
            return

        latest = BotStateRepository(self._session).get_latest(self._bot_run_id)
        current = resolve_persisted_state(latest.state if latest is not None else None)
        if current is None:
            # Dato corrupto en bot_state: no hay forma segura de mapearlo a una
            # transicion valida (mismo criterio que routes_kill_switch._current_bot_state,
            # que ante esto devuelve 500 en vez de fabricar un estado falso). Acá no hay
            # HTTP al que responder — el caller es CycleRunner._tick(), sin try/except
            # propio — asi que la falla debe quedar contenida acá: loguear y no disparar,
            # en vez de dejar el ValueError sin atrapar y tumbar el loop del worker entero.
            _log.error(
                "orphan_order_scanner.unknown_persisted_state",
                bot_run_id=self._bot_run_id,
                state=latest.state if latest is not None else None,
            )
            self._session.rollback()
            return
        if current != self._state_machine.state:
            self._state_machine.force_set(current, reason="orphan_scanner_resync")
        if current != BotState.ACTIVE:
            # El estado cambio (ej. kill switch manual) entre el ultimo _sync_state_from_db
            # del ciclo y este lock. No hay nada que disparar: ya no esta ACTIVE.
            _log.warning("orphan_order_scanner.state_changed_before_lock", state=current.value)
            self._session.rollback()
            return

        if not self._state_machine.can_transition_to(BotState.SAFE_MODE):
            # En la practica inalcanzable (ACTIVE -> SAFE_MODE es incondicional en
            # _ALLOWED_TRANSITIONS), pero valida ANTES de escribir nada: fallar acá
            # evita persistir una fila bot_state para una transicion que después
            # rechazaria transition_to(), lo que dejaria DB y memoria divergiendo
            # (ver el bug de orden persist/mutate mas abajo).
            _log.error("orphan_order_scanner.invalid_transition", current=current.value)
            self._session.rollback()
            return

        reason = "Ordenes huerfanas detectadas: " + "; ".join(
            f"{f.symbol}:{f.reason.value}" for f in findings
        )
        now = datetime.now(UTC)
        BotStateRepository(self._session).save(
            BotStateRow(
                bot_run_id=self._bot_run_id,
                state=BotState.SAFE_MODE.value,
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
        # El estado en memoria se refleja SOLO despues de que el commit tuvo
        # exito (review PR #121): si el persist fallara arriba, self._state_machine
        # debe seguir en ACTIVE para no divergir de la DB — antes se llamaba
        # transition_to() previo al commit, y un fallo de persistencia dejaba la
        # memoria en SAFE_MODE mientras la DB seguia en ACTIVE, revertida en
        # silencio por el proximo _sync_state_from_db. force_set() en vez de
        # transition_to() porque la validez de la transicion ya se confirmo
        # arriba con can_transition_to(); no hay camino de excepcion acá, y uno
        # nuevo (InvalidStateTransitionError post-commit) seria peor: DB ya en
        # SAFE_MODE con memoria sin actualizar.
        self._state_machine.force_set(BotState.SAFE_MODE, reason=reason)
        _log.warning(
            "orphan_order_scanner.safe_mode_triggered",
            reason=reason,
            findings_count=len(findings),
        )


__all__ = ["OrphanOrderScanner"]
