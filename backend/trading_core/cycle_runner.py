"""Cycle runner: ejecuta el loop principal del bot.

Cada iteracion del ciclo:
  1) consulta el state machine para saber si debe seguir corriendo
  2) registra heartbeat (archivo + log estructurado)
  3) obtiene y persiste MarketSnapshots reales via market_data_service, si
     esta inyectado (CR) — incluye Quant Signals + Regime + Volatility (F5/F6)
  4) tickea el PositionManager via position_tick_service, si esta inyectado (F14)
  5) ejecuta el pipeline de decision (GPT → Aggregator → Risk → Execution) para
     cada snapshot valido, si las dependencias de decision estan inyectadas (CR)
  6) duerme el intervalo configurado, respondiendo rapido a shutdown

El pipeline de decision es opcional: si alguna dependencia falta (gpt_client,
aggregator, config, session o bot_run_id es None), el ciclo sigue corriendo
market data y position management sin intentar nuevas entradas.
"""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from pathlib import Path

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.connection_health.monitor import ConnectionHealthMonitor
from backend.core.config import AppConfig
from backend.decision_engine.aggregator import DecisionAggregator
from backend.decision_engine.aggregator_schemas import DecisionAggregationResult
from backend.decision_engine.gpt_client import GPTClient, GPTRequest, RequestPurpose
from backend.decision_engine.prompt_builder import AccountContext, PromptBuilder, PromptContext
from backend.decision_engine.schemas import DecisionType, ModelDecision
from backend.execution.engine import ExecutionEngine
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.schemas import MarketSnapshot
from backend.market_regime.engine import MarketRegimeEngine
from backend.orphan_order_scanner.scanner import OrphanOrderScanner
from backend.position_manager.tick_service import PositionTickService
from backend.quant_signals.engine import compute_quant_signals
from backend.risk_engine import engine as risk_engine
from backend.risk_engine.schemas import RiskDecision, RiskValidationResult
from backend.storage.repositories.bot import BotStateRepository
from backend.storage.repositories.trades import TradeRepository
from backend.trading_core.bot_state_machine import BotStateMachine, resolve_persisted_state
from backend.volatility.engine import compute_volatility_assessment

log = structlog.get_logger(__name__)

DEFAULT_HEARTBEAT_FILE = Path("/tmp/worker_alive")
DEFAULT_INTERVAL_SECONDS = 10


class CycleRunner:
    """Ejecuta el loop principal hasta que se senale shutdown.

    El shutdown se hace via `request_shutdown()` (thread-safe). El loop
    chequea la senal en intervalos cortos para terminar rapido ante SIGTERM.
    """

    def __init__(
        self,
        state_machine: BotStateMachine,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        heartbeat_file: Path = DEFAULT_HEARTBEAT_FILE,
        position_tick_service: PositionTickService | None = None,
        orphan_order_scanner: OrphanOrderScanner | None = None,
        connection_health_monitor: ConnectionHealthMonitor | None = None,
        market_data_service: MarketDataCycleService | None = None,
        execution_engine: ExecutionEngine | None = None,
        gpt_client: GPTClient | None = None,
        prompt_builder: PromptBuilder | None = None,
        aggregator: DecisionAggregator | None = None,
        config: AppConfig | None = None,
        session: Session | None = None,
        bot_run_id: str | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds}")
        self._state_machine = state_machine
        self._interval_seconds = interval_seconds
        self._heartbeat_file = heartbeat_file
        self._position_tick_service = position_tick_service
        self._orphan_order_scanner = orphan_order_scanner
        self._connection_health_monitor = connection_health_monitor
        self._market_data_service = market_data_service
        self._execution_engine = execution_engine
        self._shutdown_event = threading.Event()

        # Decision pipeline dependencies
        self._gpt_client = gpt_client
        self._prompt_builder = prompt_builder
        self._aggregator = aggregator
        self._config = config
        self._session = session
        self._bot_run_id = bot_run_id
        if self._session is None or self._bot_run_id is None:
            # Sin esto el kill switch queda inerte sin ninguna señal: hoy solo
            # pasa en tests (Orchestrator en produccion siempre inyecta ambos),
            # pero un cambio de wiring futuro podria apagarlo en silencio.
            log.warning("cycle_runner.kill_switch_sync_disabled")

        # Auxiliary components for the decision pipeline
        self._regime_engine: MarketRegimeEngine | None = None
        self._trade_repo: TradeRepository | None = None
        if self._decision_pipeline_ready:
            self._regime_engine = MarketRegimeEngine()
            self._trade_repo = TradeRepository(session)  # type: ignore[arg-type]

        log.info(
            "cycle_runner.init",
            interval_seconds=interval_seconds,
            heartbeat_file=str(heartbeat_file),
            decision_pipeline=self._decision_pipeline_ready,
        )

    @property
    def _decision_pipeline_ready(self) -> bool:
        return (
            self._gpt_client is not None
            and self._prompt_builder is not None
            and self._aggregator is not None
            and self._config is not None
            and self._session is not None
            and self._bot_run_id is not None
            and self._execution_engine is not None
        )

    def request_shutdown(self) -> None:
        """Senala al loop que debe terminar. Idempotente."""
        if not self._shutdown_event.is_set():
            log.info("cycle_runner.shutdown_requested")
        self._shutdown_event.set()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    @property
    def heartbeat_file(self) -> Path:
        return self._heartbeat_file

    @property
    def execution_engine(self) -> ExecutionEngine | None:
        return self._execution_engine

    def run(self) -> None:
        """Loop principal. Bloquea hasta que se pida shutdown."""
        log.info(
            "cycle_runner.start",
            state=self._state_machine.state.value,
            interval_seconds=self._interval_seconds,
        )
        while not self._shutdown_event.is_set():
            self._sync_state_from_db()
            if not self._state_machine.is_running():
                # El proceso esta vivo, solo pausado por estado (HALTED /
                # KILL_SWITCH_TRIGGERED). Igual hay que refrescar el heartbeat:
                # es el caso normal tras un kill switch + restart del worker, y
                # sin esto el healthcheck del container (docker-compose.yml:
                # `find /tmp/worker_alive -mmin -2`) lo marca unhealthy para
                # siempre aunque no lo este (F16 [157]).
                self._touch_heartbeat()
                log.info("cycle_runner.paused_by_state", state=self._state_machine.state.value)
            else:
                self._tick()
            # Espera segmentada para responder rapido a shutdown.
            if self._shutdown_event.wait(timeout=self._interval_seconds):
                break
        log.info("cycle_runner.stopped")

    def _sync_state_from_db(self) -> None:
        """Relee el estado persistido en bot_state antes de cada iteracion del while
        y antes de cada simbolo del pipeline de decision (ver _run_decision_pipeline).

        El kill switch manual corre en el proceso de la API, con su propia
        BotStateMachine en memoria: sin esto, este loop nunca se entera de un
        kill switch disparado desde el dashboard y sigue operando aunque la
        API ya muestre KILL_SWITCH_TRIGGERED. No-op si no hay sesion/bot_run_id
        inyectados (tests unitarios sin DB, o CycleRunner con dependencias de
        decision no wireadas).
        """
        if self._session is None or self._bot_run_id is None:
            return
        try:
            latest = BotStateRepository(self._session).get_latest(self._bot_run_id)
        except SQLAlchemyError:
            # Fail-open, misma razon que el ValueError de mas abajo: frenar el
            # tick por un error transitorio de DB (conexion caida, timeout) no
            # es obviamente mejor que seguir operando con el ultimo estado
            # local conocido, y dejar la excepcion sin atrapar tumbaria el
            # loop entero — sin log ni oportunidad de recuperarse en el
            # proximo ciclo.
            log.error(
                "cycle_runner.state_sync_db_error", bot_run_id=self._bot_run_id, exc_info=True
            )
            self._session.rollback()
            return
        if latest is None:
            return
        persisted = resolve_persisted_state(latest.state)
        if persisted is None:
            # Fail-open a proposito, asimetrico respecto del endpoint (que
            # devuelve 500 ante el mismo dato corrupto en
            # routes_kill_switch._current_bot_state): frenar el tick acá
            # dejaria posiciones abiertas sin gestionar, que no es obviamente
            # mejor que seguir operando con el ultimo estado local conocido.
            # Si esta asimetria deja de ser aceptable, revisar junto con el
            # endpoint en vez de cambiar solo un lado.
            log.error(
                "cycle_runner.unknown_persisted_state",
                bot_run_id=self._bot_run_id,
                state=latest.state,
            )
            return
        if persisted != self._state_machine.state:
            self._state_machine.force_set(persisted, reason="synced_from_db")

    def _touch_heartbeat(self) -> None:
        """Marca el proceso como vivo para el healthcheck del container.

        Se llama tanto en el tick normal como en la rama paused_by_state: el
        healthcheck de docker-compose solo mira la mtime de este archivo, asi
        que mientras el loop siga girando (aunque el estado no sea ACTIVE) el
        worker debe reportarse healthy.
        """
        self._heartbeat_file.touch(exist_ok=True)

    def _tick(self) -> None:
        """Una iteracion del ciclo: heartbeat + market data + posiciones + decision pipeline.

        Market data se tickea antes que posiciones y que el pipeline de decision
        para que cada capa trabaje con el snapshot mas reciente. MarketDataCycleService
        y PositionTickService aislan fallas por simbolo internamente. El pipeline
        de decision también aísla fallas por símbolo para mantener el heartbeat vivo.
        El monitor de salud de conexion corre justo despues de market data (F16
        [117]): evalua los snapshots recien obtenidos (conectividad/clock skew/
        latencia) antes de que posiciones u ordenes se gestionen sobre ese ciclo.
        El scanner de ordenes huerfanas corre despues de tickear posiciones (F16
        [115]): reconcilia contra el estado ya actualizado de ese ciclo, y aisla
        fallas por simbolo internamente igual que los demas servicios tickeados.
        """
        self._touch_heartbeat()
        log.info("cycle_runner.heartbeat", state=self._state_machine.state.value)

        snapshots: list[MarketSnapshot] = []
        if self._market_data_service is not None:
            snapshots = self._market_data_service.tick_all()
        if self._connection_health_monitor is not None:
            self._connection_health_monitor.check_and_enforce(snapshots)
        if self._position_tick_service is not None:
            self._position_tick_service.tick_all()
        if self._orphan_order_scanner is not None:
            self._orphan_order_scanner.scan_and_enforce()

        if self._decision_pipeline_ready and snapshots:
            asyncio.run(self._run_decision_pipeline(snapshots))

    # ------------------------------------------------------------------
    # Decision pipeline
    # ------------------------------------------------------------------

    async def _run_decision_pipeline(self, snapshots: list[MarketSnapshot]) -> None:
        """Ejecuta GPT → Aggregator → Risk → Execution para cada snapshot valido.

        Los simbolos se procesan secuencialmente para evitar concurrencia sobre
        la sesion de SQLAlchemy y el estado del PaperAdapter (no thread-safe).
        Las fallas por simbolo se aislan: una excepcion no bloquea el resto.
        Un SAVEPOINT por simbolo garantiza que el rollback de un error solo
        deshace el trabajo de ese simbolo, sin afectar audit rows ya persistidas
        de simbolos anteriores en el mismo tick.

        `_sync_state_from_db` solo corria entre iteraciones del while de run(),
        no dentro de un tick — y con varios simbolos, cada uno con su propia
        llamada a GPT (hasta ~30s + reintentos con backoff), un tick puede
        durar minutos. Sin el chequeo de abajo, un kill switch disparado a
        mitad de tick no frenaba nada hasta la proxima vuelta del while: el
        pipeline seguia abriendo posiciones para los simbolos que faltaban.
        Resincronizamos antes de cada simbolo (no solo al principio del
        pipeline): es la unica forma de que el chequeo detecte un kill switch
        disparado mientras el simbolo anterior estaba en medio de su llamada
        a GPT. Es un SELECT extra por simbolo contra una tabla indexada por
        bot_run_id — al lado de una llamada a GPT no se nota.

        can_trade() se chequea aparte de is_running() (PDF 4.8: solo ACTIVE abre
        posiciones nuevas; SAFE_MODE administra las existentes pero no abre otras).
        A diferencia de is_running(), que aborta el resto del tick entero, un
        can_trade()==False solo saltea ese simbolo y sigue con el resto: SAFE_MODE
        no detiene el loop (is_running() sigue True), asi que PositionTickService
        debe seguir gestionando salidas de simbolos posteriores en el mismo tick.
        """
        assert self._session is not None
        for snapshot in snapshots:
            self._sync_state_from_db()
            if not self._state_machine.is_running():
                log.info(
                    "cycle_runner.pipeline_aborted_by_state",
                    state=self._state_machine.state.value,
                    remaining_symbol=snapshot.symbol,
                )
                return
            if not self._state_machine.can_trade():
                log.info(
                    "cycle_runner.new_entries_blocked_by_state",
                    state=self._state_machine.state.value,
                    symbol=snapshot.symbol,
                )
                continue
            try:
                with self._session.begin_nested():
                    await self._process_symbol(snapshot)
            except Exception:
                log.error(
                    "cycle_runner.decision_pipeline_error",
                    symbol=snapshot.symbol,
                    exc_info=True,
                )

    async def _process_symbol(self, snapshot: MarketSnapshot) -> None:
        """Pipeline completo para un simbolo: GPT → Aggregator → Risk → Execution."""
        assert self._gpt_client is not None
        assert self._prompt_builder is not None
        assert self._aggregator is not None
        assert self._config is not None
        assert self._session is not None
        assert self._bot_run_id is not None
        assert self._execution_engine is not None
        assert self._regime_engine is not None
        assert self._trade_repo is not None

        symbol = snapshot.symbol

        # 1. Derivar señales deterministas del snapshot (idéntico a MarketAnalysisService)
        quant = compute_quant_signals(snapshot)
        regime = self._regime_engine.assess(snapshot)
        volatility = compute_volatility_assessment(snapshot)

        # 2. Obtener pérdidas acumuladas del DB para construir el AccountContext y el Risk Engine
        daily_loss, total_loss = self._trade_repo.get_loss_totals(self._bot_run_id)
        initial_balance = Decimal(str(self._config.challenge.initial_balance_usdt))
        daily_drawdown_pct = float(daily_loss / initial_balance * 100) if initial_balance else 0.0

        account = AccountContext(
            environment=self._config.execution.environment.value,
            balance_usdt=float(snapshot.account_balance_usdt),
            open_positions_count=snapshot.open_positions_count,
            daily_drawdown_percent=daily_drawdown_pct,
            max_leverage_for_environment=self._config.leverage.max_leverage_paper,
        )
        ctx = PromptContext(
            snapshot=snapshot,
            quant_signals=quant,
            regime=regime,
            volatility=volatility,
            account=account,
        )
        system_prompt, user_prompt = self._prompt_builder.build(ctx)
        req = GPTRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=ctx.prompt_version,
        )

        # 3. Llamar a GPT (async, con retry/timeout según failure_policy)
        gpt_decision: ModelDecision | None = await self._gpt_client.request(
            req,
            RequestPurpose.NEW_ENTRY,
            session=self._session,
            bot_run_id=self._bot_run_id,
            symbol=symbol,
        )
        if gpt_decision is None:
            # failure_policy swallowed el error (gpt_failure_blocks_new_entries=False)
            log.warning("cycle_runner.gpt_returned_none", symbol=symbol)
            return

        # 4. Decision Aggregator — combina GPT + Quant + Regime + Volatility
        aggregation: DecisionAggregationResult = self._aggregator.aggregate(
            gpt_decision, quant, regime, volatility
        )

        # 5. Gating critico: si el Aggregator dice NO_OPERAR, no ejecutar.
        # El Risk Engine evalua decision.execute (del ModelDecision original), no
        # aggregation.final_action — por eso el caller debe verificar esto primero.
        if aggregation.final_action == DecisionType.NO_OPERAR:
            log.info(
                "cycle_runner.no_operar",
                symbol=symbol,
                score=aggregation.aggregated_score,
                contradictions=aggregation.contradictions_detected,
            )
            return

        # 6. Risk Engine — valida parámetros del trade con datos de pérdida reales
        last_trade = self._trade_repo.get_last_closed_trade(self._bot_run_id, symbol)
        open_position_pnl = self._execution_engine.get_open_position_unrealized_pnl(symbol)
        risk_result: RiskValidationResult = risk_engine.validate(
            aggregation=aggregation,
            decision=gpt_decision,
            daily_loss_usdt=daily_loss,
            total_loss_usdt=total_loss,
            config=self._config,
            last_trade_pnl_usdt=last_trade.net_pnl if last_trade else None,
            last_trade_margin_usdt=last_trade.margin_usdt if last_trade else None,
            open_position_unrealized_pnl_usdt=open_position_pnl,
        )

        # 7. Ejecutar si el Risk Engine aprueba o ajusta
        if risk_result.decision in (RiskDecision.APPROVE, RiskDecision.ADJUST_DOWN):
            log.info(
                "cycle_runner.executing_plan",
                symbol=symbol,
                risk_decision=risk_result.decision.value,
                direction=gpt_decision.decision.value,
            )
            self._execution_engine.execute_approved_plan(gpt_decision, risk_result)
        else:
            log.info(
                "cycle_runner.risk_blocked",
                symbol=symbol,
                risk_decision=risk_result.decision.value,
                reasons=list(risk_result.reasons.keys()),
            )


def parse_interval_from_env(raw: str | None, default: int = DEFAULT_INTERVAL_SECONDS) -> int:
    """Convierte la variable de entorno a int con validacion.

    Lanza ValueError con mensaje claro si el valor no es un int positivo,
    para que el orchestrator falle rapido al arrancar en vez de en runtime.
    """
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"WORKER_HEARTBEAT_INTERVAL_SECONDS must be an int, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"WORKER_HEARTBEAT_INTERVAL_SECONDS must be > 0, got {value}")
    return value


__all__ = [
    "DEFAULT_HEARTBEAT_FILE",
    "DEFAULT_INTERVAL_SECONDS",
    "CycleRunner",
    "parse_interval_from_env",
]
