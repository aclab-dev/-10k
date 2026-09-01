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

Ante cualquier hallazgo con el bot en ACTIVE, dispara SAFE_MODE con el mismo patrón
de OrphanOrderScanner (F16 [115]): lock de fila del BotRun, transición validada por
la state machine, persistencia atómica de BotState + SystemEvent. Duplicado a
propósito en vez de refactorizar OrphanOrderScanner para compartirlo: evita mezclar
un refactor de código ya aprobado con esta feature.

Sin contador de ticks consecutivos: para cuando MarketDataCycleService reporta un
hallazgo, ese fetch ya sobrevivió los reintentos con backoff de F16 [113] (o el
circuit breaker ya está abierto) — un solo tick con hallazgo ya es señal real, no
ruido transitorio. Mismo criterio que OrphanOrderScanner.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.connection_health.schemas import ConnectionAnomalyFinding, ConnectionAnomalyReason
from backend.market_data.schemas import ALLOWED_SYMBOLS, MarketSnapshot
from backend.storage.models import BotRun, SystemEvent
from backend.storage.models import BotState as BotStateRow
from backend.storage.repositories.audit import SystemEventRepository
from backend.storage.repositories.bot import BotStateRepository
from backend.trading_core.bot_state_machine import (
    BotState,
    BotStateMachine,
    resolve_persisted_state,
)

_log = structlog.get_logger(__name__)


class ConnectionHealthMonitor:
    """Evalúa, en cada ciclo, la salud de conexión/reloj de los snapshots recibidos
    y aplica SAFE_MODE si hace falta.

    No thread-safe, mismo criterio que el resto de los componentes tickeados del
    worker (OrphanOrderScanner, PositionTickService): se asume un solo loop
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
            # Fail-open igual que OrphanOrderScanner.scan_and_enforce: un error
            # transitorio de DB no debe tumbar el tick entero. El proximo ciclo
            # vuelve a intentarlo.
            _log.error("connection_health_monitor.safe_mode_persist_failed", exc_info=True)
            self._session.rollback()

        return findings

    def _trigger_safe_mode(self, findings: list[ConnectionAnomalyFinding]) -> None:
        # Mismo patron de lock/persist que OrphanOrderScanner._trigger_safe_mode:
        # lock de fila evita una carrera con un kill switch manual disparado
        # desde la API en el mismo instante. A partir de aca, TODO return debe
        # soltar la transaccion (rollback).
        bot_run = self._session.get(BotRun, self._bot_run_id, with_for_update=True)
        if bot_run is None or bot_run.status != "RUNNING":
            _log.warning(
                "connection_health_monitor.bot_run_not_running",
                bot_run_id=self._bot_run_id,
                status=bot_run.status if bot_run else None,
            )
            self._session.rollback()
            return

        latest = BotStateRepository(self._session).get_latest(self._bot_run_id)
        current = resolve_persisted_state(latest.state if latest is not None else None)
        if current is None:
            _log.error(
                "connection_health_monitor.unknown_persisted_state",
                bot_run_id=self._bot_run_id,
                state=latest.state if latest is not None else None,
            )
            self._session.rollback()
            return
        if current != self._state_machine.state:
            self._state_machine.force_set(current, reason="connection_health_resync")
        if current != BotState.ACTIVE:
            # El estado cambio (ej. kill switch manual) entre el ultimo
            # _sync_state_from_db del ciclo y este lock. No hay nada que
            # disparar: ya no esta ACTIVE.
            _log.warning("connection_health_monitor.state_changed_before_lock", state=current.value)
            self._session.rollback()
            return

        if not self._state_machine.can_transition_to(BotState.SAFE_MODE):
            _log.error("connection_health_monitor.invalid_transition", current=current.value)
            self._session.rollback()
            return

        reason = "Anomalias de conexion/reloj detectadas: " + "; ".join(
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
                event_type="CONNECTION_HEALTH_ANOMALY",
                severity="WARNING",
                message=reason,
                details={"findings": [f.model_dump(mode="json") for f in findings]},
            )
        )
        self._session.commit()
        # El estado en memoria se refleja SOLO despues de que el commit tuvo
        # exito (mismo criterio que OrphanOrderScanner, ver review PR #121):
        # si el persist fallara arriba, self._state_machine debe seguir en
        # ACTIVE para no divergir de la DB.
        self._state_machine.force_set(BotState.SAFE_MODE, reason=reason)
        _log.warning(
            "connection_health_monitor.safe_mode_triggered",
            reason=reason,
            findings_count=len(findings),
        )


__all__ = ["ConnectionHealthMonitor"]
