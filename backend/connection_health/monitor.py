"""ConnectionHealthMonitor — detección de pérdida de conexión, clock skew y latencia (F16 [117]).

Consume, cada ciclo, la lista de MarketSnapshot que MarketDataCycleService.tick_all()
ya obtuvo y validó. No hace I/O propio contra el exchange: toda la señal sale de datos
que el pipeline de market data ya calculó (latency_ms, clock_skew_ms — ver
backend/market_data/bingx_fetcher.py), evitando acoplar este módulo a un exchange
concreto o duplicar el timing de F16 [113].

Dos tipos de anomalía:
- SYMBOL_DATA_UNAVAILABLE: el símbolo no aparece entre los snapshots exitosos del
  ciclo. Puede ser un fallo de transporte (ya agotó los reintentos + circuit breaker
  de F16 [113]) o un rechazo del Market Data Guard (stale/incoherente/clock skew
  fuera del bound duro ±5000ms de MarketSnapshot) — no distinguimos la causa exacta
  acá, MarketDataCycleService ya la logueó en detalle.
- CLOCK_SKEW_EXCEEDED / LATENCY_EXCEEDED: el snapshot sí llegó (dentro del bound
  duro de Pydantic) pero clock_skew_ms/latency_ms superan el umbral configurado
  (ConnectionHealthConfig), más estricto que ese bound duro — alerta temprana antes
  del rechazo.

Ante cualquier hallazgo con el bot en ACTIVE, dispara SAFE_MODE vía
EmergencyStopService (F16 [158]): lock de fila del BotRun, transición validada por
la state machine, persistencia atómica de BotState + SystemEvent. Mismo servicio que
usan routes_kill_switch.py y ReconciliationGate (F16 [159]) — antes de F16 [158]
este módulo duplicaba el patrón por tercera vez.

Sin contador de ticks consecutivos: para cuando MarketDataCycleService reporta un
hallazgo, ese fetch ya sobrevivió los reintentos con backoff de F16 [113] (o el
circuit breaker ya está abierto) — un solo tick con hallazgo ya es señal real, no
ruido transitorio. Mismo criterio que ReconciliationGate.
"""

from __future__ import annotations

from datetime import datetime

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.connection_health.schemas import ConnectionAnomalyFinding, ConnectionAnomalyReason
from backend.market_data.schemas import ALLOWED_SYMBOLS, MarketSnapshot
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


class ConnectionHealthMonitor:
    """Evalúa, en cada ciclo, la salud de conexión/reloj de los snapshots recibidos
    y aplica SAFE_MODE si hace falta.

    No thread-safe, mismo criterio que el resto de los componentes tickeados del
    worker (ReconciliationGate, PositionTickService): se asume un solo loop
    llamando check_and_enforce() secuencialmente.
    """

    def __init__(
        self,
        state_machine: BotStateMachine,
        session: Session,
        bot_run_id: str,
        *,
        max_clock_skew_ms: int,
        max_latency_ms: int,
        symbols: frozenset[str] | None = None,
    ) -> None:
        self._state_machine = state_machine
        self._session = session
        self._bot_run_id = bot_run_id
        self._max_clock_skew_ms = max_clock_skew_ms
        self._max_latency_ms = max_latency_ms
        self._symbols = frozenset(symbols or ALLOWED_SYMBOLS)

    def check_all(self, snapshots: list[MarketSnapshot]) -> list[ConnectionAnomalyFinding]:
        """Evalúa los snapshots exitosos de un ciclo contra los símbolos esperados.

        No hace I/O: toda la evaluación es sobre datos ya obtenidos por
        MarketDataCycleService.tick_all() en el mismo ciclo.
        """
        findings: list[ConnectionAnomalyFinding] = []
        by_symbol = {s.symbol: s for s in snapshots}

        for symbol in sorted(self._symbols):
            snapshot = by_symbol.get(symbol)
            if snapshot is None:
                findings.append(
                    ConnectionAnomalyFinding(
                        symbol=symbol,
                        reason=ConnectionAnomalyReason.SYMBOL_DATA_UNAVAILABLE,
                        detail=(
                            f"Sin snapshot exitoso para {symbol} en este ciclo "
                            "(fetch fallido o snapshot rechazado)"
                        ),
                    )
                )
                continue
            if abs(snapshot.clock_skew_ms) > self._max_clock_skew_ms:
                findings.append(
                    ConnectionAnomalyFinding(
                        symbol=symbol,
                        reason=ConnectionAnomalyReason.CLOCK_SKEW_EXCEEDED,
                        detail=(
                            f"clock_skew_ms={snapshot.clock_skew_ms} supera el umbral de "
                            f"{self._max_clock_skew_ms} ms"
                        ),
                    )
                )
            if snapshot.latency_ms > self._max_latency_ms:
                findings.append(
                    ConnectionAnomalyFinding(
                        symbol=symbol,
                        reason=ConnectionAnomalyReason.LATENCY_EXCEEDED,
                        detail=(
                            f"latency_ms={snapshot.latency_ms} supera el umbral de "
                            f"{self._max_latency_ms} ms"
                        ),
                    )
                )

        return findings

    def check_and_enforce(self, snapshots: list[MarketSnapshot]) -> list[ConnectionAnomalyFinding]:
        """Evalúa y, si hay hallazgos y el bot está ACTIVE, dispara SAFE_MODE.

        Si ya está en SAFE_MODE (u otro estado no-ACTIVE) mientras la anomalía
        persiste, solo loguea — no reintenta una transición inválida ni duplica
        el SystemEvent en cada tick.
        """
        findings = self.check_all(snapshots)
        if not findings:
            _log.info("connection_health_monitor.cycle_clean", symbols_checked=len(self._symbols))
            return findings

        if self._state_machine.state != BotState.ACTIVE:
            _log.warning(
                "connection_health_monitor.findings_while_not_active",
                state=self._state_machine.state.value,
                findings=[f.model_dump(mode="json") for f in findings],
            )
            return findings

        try:
            self._trigger_safe_mode(findings)
        except SQLAlchemyError:
            # Fail-open igual que ReconciliationGate.run_and_enforce: un error
            # transitorio de DB no debe tumbar el tick entero. El proximo ciclo
            # vuelve a intentarlo.
            _log.error("connection_health_monitor.safe_mode_persist_failed", exc_info=True)
            self._session.rollback()

        return findings

    def _trigger_safe_mode(self, findings: list[ConnectionAnomalyFinding]) -> None:
        reason = "Anomalias de conexion/reloj detectadas: " + "; ".join(
            f"{f.symbol}:{f.reason.value}" for f in findings
        )

        def _audit_event(bot_run_id: str, now: datetime, _previous: BotState) -> SystemEvent:
            return SystemEvent(
                bot_run_id=bot_run_id,
                timestamp=now,
                event_type="CONNECTION_HEALTH_ANOMALY",
                severity="WARNING",
                message=reason,
                details={"findings": [f.model_dump(mode="json") for f in findings]},
            )

        try:
            EmergencyStopService(self._session).trigger(
                bot_run_id=self._bot_run_id,
                target=BotState.SAFE_MODE,
                reason=reason,
                audit_event_factory=_audit_event,
                require_current_in=frozenset({BotState.ACTIVE}),
                state_machine=self._state_machine,
                resync_reason="connection_health_resync",
            )
        except BotRunNotRunningError as exc:
            _log.warning(
                "connection_health_monitor.bot_run_not_running",
                bot_run_id=exc.bot_run_id,
                status=exc.status,
            )
        except UnknownPersistedStateError as exc:
            _log.error(
                "connection_health_monitor.unknown_persisted_state",
                bot_run_id=exc.bot_run_id,
                state=exc.raw_state,
            )
        except CurrentStateNotAllowedError as exc:
            # El estado cambio (ej. kill switch manual) entre el ultimo
            # _sync_state_from_db del ciclo y este lock. No hay nada que
            # disparar: ya no esta ACTIVE.
            _log.warning(
                "connection_health_monitor.state_changed_before_lock", state=exc.current.value
            )
        except InvalidStateTransitionError as exc:
            _log.error("connection_health_monitor.invalid_transition", current=exc.current.value)
        else:
            _log.warning(
                "connection_health_monitor.safe_mode_triggered",
                reason=reason,
                findings_count=len(findings),
            )


__all__ = ["ConnectionHealthMonitor"]
