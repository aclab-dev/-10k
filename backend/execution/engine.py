"""ExecutionEngine — ejecuta un ApprovedTradePlan real (F10/CR).

"ApprovedTradePlan" = `ModelDecision` (F7) + `RiskValidationResult` ejecutable
(F9, decision en {APPROVE, ADJUST_DOWN}). No existe un tipo literal con ese
nombre en el codebase (vocabulario de la spec, sección 3.7/4.4); este módulo
combina ambos objetos existentes.

Solo PAPER. Construye la orden, la coloca vía `ExchangeAdapter.place_order()`
(con timeout — ver `_place_order_with_timeout`), persiste Order/Trade/Position,
y si la orden queda FILLED entrega la posición a `PositionManager.set_config()`
(handoff únicamente; el tickeo periódico es responsabilidad de F14/[143]).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from decimal import ROUND_DOWN, Decimal

import structlog
from sqlalchemy.orm import Session

from backend.core.config import Environment, PositionManagementConfig
from backend.decision_engine.schemas import DecisionType, EntryType, ModelDecision
from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from backend.execution.schemas import ExecutionResult
from backend.position_manager.config_mapper import build_position_config
from backend.position_manager.manager import PositionManager
from backend.risk_engine.schemas import (
    EXECUTABLE_RISK_DECISIONS,
    AdjustedParameters,
    RiskValidationResult,
)
from backend.storage.models import Order, Position, Trade
from backend.storage.repositories.snapshots import VolatilityAssessmentRepository
from backend.storage.repositories.trades import OrderRepository

log = structlog.get_logger(__name__)

_QUANT = Decimal("0.00000001")


class ExecutionTimeoutError(Exception):
    """La llamada bloqueante a adapter.place_order() no respondió dentro del timeout."""


class ExecutionEngine:
    """Ejecuta un ApprovedTradePlan (ModelDecision + RiskValidationResult) en PAPER."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        position_manager: PositionManager,
        session: Session,
        bot_run_id: str,
        environment: Environment,
        position_management_defaults: PositionManagementConfig,
        place_order_timeout_seconds: float,
    ) -> None:
        self._adapter = adapter
        self._position_manager = position_manager
        self._session = session
        self._bot_run_id = bot_run_id
        self._environment = environment
        self._position_management_defaults = position_management_defaults
        self._timeout_seconds = place_order_timeout_seconds
        self._order_repo = OrderRepository(session)
        self._volatility_repo = VolatilityAssessmentRepository(session)
        # Un solo worker: place_order() de PaperAdapter no es thread-safe para
        # llamadas concurrentes (ver docstring de PaperAdapter), así que un pool
        # más grande no aportaría paralelismo real, solo complejidad.
        self._executor = ThreadPoolExecutor(max_workers=1)

    def close(self) -> None:
        """Libera el thread pool interno. Idempotente."""
        self._executor.shutdown(wait=False)

    def execute_approved_plan(
        self, decision: ModelDecision, risk_result: RiskValidationResult
    ) -> ExecutionResult:
        """Ejecuta un plan aprobado: coloca la orden y, si llena, registra la posición.

        Idempotente: si ya existe una Order persistida con este client_order_id
        (mismo decision_id — un retry del mismo plan aprobado), devuelve el
        resultado ya registrado sin re-ejecutar ni duplicar (el adapter es
        idempotente en memoria via PaperAdapter.place_order, pero eso no evita
        un segundo INSERT en `orders`; acá se chequea la DB primero).

        Raises:
            ValueError: risk_result no ejecutable, entry_type inválido, o no hay
                dato de volatilidad (ATR) para el símbolo — en ese caso no se
                coloca ninguna orden (fail closed: sin ATR no hay trailing/SL
                confiable, "sin SL no se opera").
            ExecutionTimeoutError: adapter.place_order() excedió el timeout.
        """
        # SELECT + INSERT (en _persist_order, más abajo) no son atómicos: dos
        # llamadas concurrentes con el mismo decision_id podrían pasar ambas
        # este chequeo y competir por orders.uq_orders_client_order_id. No es
        # explotable hoy porque las decisiones se ejecutan desde un ciclo
        # single-threaded (CycleRunner._tick()) — si en el futuro esto se
        # invoca desde más de un path concurrente, hace falta un lock o una
        # transacción con aislamiento serializable acá.
        existing = self._order_repo.get_by_client_order_id(decision.decision_id)
        if existing is not None:
            log.info(
                "execution_engine.idempotent_replay",
                client_order_id=existing.client_order_id,
                order_db_id=existing.id,
            )
            return self._execution_result_from_existing_order(existing)

        if risk_result.decision not in EXECUTABLE_RISK_DECISIONS:
            raise ValueError(
                f"RiskValidationResult.decision={risk_result.decision} no es ejecutable "
                f"(validation_id={risk_result.validation_id})"
            )
        if decision.entry_type == EntryType.NO_ENTRY:
            raise ValueError(
                f"ModelDecision.entry_type=NO_ENTRY no puede ejecutarse "
                f"(decision_id={decision.decision_id})"
            )
        adjusted = risk_result.adjusted_parameters
        if adjusted is None:
            # Garantizado no-None para EXECUTABLE_RISK_DECISIONS por el validator
            # de RiskValidationResult; guard defensivo, no confiar ciegamente en
            # el invariante de otro módulo.
            raise RuntimeError(
                f"RiskValidationResult.decision={risk_result.decision} sin "
                f"adjusted_parameters (validation_id={risk_result.validation_id})"
            )
        if decision.entry_price <= 0:
            raise ValueError(f"entry_price debe ser > 0, recibido {decision.entry_price}")

        # Chequeo ANTES de tocar el adapter: sin ATR no se puede registrar un
        # trailing stop confiable con la config default (trailing_default_mode=ATR).
        # Fail closed en vez de abrir una posición sin protección registrable.
        atr_1h = self._latest_atr(decision.symbol)
        if atr_1h is None:
            raise ValueError(
                f"No hay VolatilityAssessment reciente para {decision.symbol} "
                f"(bot_run_id={self._bot_run_id}) — no se ejecuta sin dato de ATR."
            )

        self._adapter.set_margin_type(decision.symbol, decision.margin_type)
        self._adapter.set_leverage(decision.symbol, adjusted.leverage)

        quantity = self._compute_quantity(adjusted.margin_usdt, adjusted.leverage, decision)
        request = self._build_order_request(decision, quantity)

        result = self._place_order_with_timeout(request)

        order_row = self._persist_order(decision, result, trade_id=None)

        if result.status != OrderStatus.FILLED:
            self._session.commit()
            return ExecutionResult(
                order_result=result,
                trade_id=None,
                order_db_id=order_row.id,
                position_registered=False,
            )

        trade_row = self._persist_trade(decision, adjusted, result)
        order_row.trade_id = trade_row.id
        self._persist_position(decision, adjusted, result, trade_row.id)
        self._register_position_manager(decision, atr_1h)

        self._session.commit()
        return ExecutionResult(
            order_result=result,
            trade_id=trade_row.id,
            order_db_id=order_row.id,
            position_registered=True,
        )

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _latest_atr(self, symbol: str) -> Decimal | None:
        assessment = self._volatility_repo.get_latest_by_symbol(self._bot_run_id, symbol)
        return assessment.atr if assessment is not None else None

    @staticmethod
    def _execution_result_from_existing_order(order_row: Order) -> ExecutionResult:
        """Reconstruye un ExecutionResult a partir de una Order ya persistida (replay idempotente).

        `slippage_usdt` no se persiste en `orders` — no es recuperable en un
        replay, se devuelve en 0 (no afecta la idempotencia: la orden real ya
        se colocó una única vez, esto es solo la respuesta al caller repetido).
        """
        status = OrderStatus(order_row.status)
        order_result = OrderResult(
            order_id=order_row.exchange_order_id or order_row.id,
            client_order_id=order_row.client_order_id,
            symbol=order_row.symbol,
            side=OrderSide(order_row.side),
            order_type=OrderType(order_row.order_type),
            status=status,
            quantity_requested=order_row.quantity,
            quantity_filled=order_row.quantity if status == OrderStatus.FILLED else Decimal("0"),
            fill_price=order_row.fill_price,
            fee_usdt=order_row.fee or Decimal("0"),
            slippage_usdt=Decimal("0"),
            is_simulated=order_row.is_simulated,
            timestamp_utc=order_row.filled_at or order_row.created_at,
        )
        return ExecutionResult(
            order_result=order_result,
            trade_id=order_row.trade_id,
            order_db_id=order_row.id,
            position_registered=order_row.trade_id is not None,
        )

    @staticmethod
    def _compute_quantity(margin_usdt: Decimal, leverage: int, decision: ModelDecision) -> Decimal:
        notional = margin_usdt * leverage
        quantity = notional / Decimal(str(decision.entry_price))
        # ROUND_DOWN: nunca excede el margen aprobado por el Risk Engine.
        return quantity.quantize(_QUANT, rounding=ROUND_DOWN)

    @staticmethod
    def _build_order_request(decision: ModelDecision, quantity: Decimal) -> OrderRequest:
        side = OrderSide.BUY if decision.decision == DecisionType.LONG else OrderSide.SELL
        is_market = decision.entry_type == EntryType.MARKET
        order_type = OrderType.MARKET if is_market else OrderType.LIMIT
        return OrderRequest(
            # Reusar decision_id como client_order_id da idempotencia gratis: si
            # execute_approved_plan se reintenta para el mismo ModelDecision,
            # PaperAdapter.place_order() devuelve el resultado previo sin duplicar.
            client_order_id=decision.decision_id,
            symbol=decision.symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            # PaperAdapter usa price como referencia para simular el fill incluso
            # en MARKET (ver _fill_market_order) — siempre requerido, no solo LIMIT.
            price=Decimal(str(decision.entry_price)),
        )

    def _place_order_with_timeout(self, request: OrderRequest) -> OrderResult:
        """Coloca la orden con un timeout real, sin dejar el pool inutilizable tras un hang.

        `ThreadPoolExecutor` no puede cancelar un thread que ya empezó a
        correr (`future.cancel()` no aplica una vez arrancado) — al hacer
        timeout, el worker sigue vivo en segundo plano ejecutando
        `adapter.place_order()`. Con `max_workers=1`, si no se descarta el
        pool, ese thread colgado bloquearía TODAS las llamadas futuras a
        `execute_approved_plan()` en esta instancia (quedarían encoladas
        detrás de él indefinidamente — un solo hang tumba el resto del ciclo
        para siempre, no solo esa llamada). Por eso, ante timeout, se
        descarta el executor y se arma uno nuevo: las llamadas subsiguientes
        no quedan atrapadas.

        Limitación que esto NO resuelve (documentada, no hay forma de
        resolverla con `ThreadPoolExecutor`): el thread huérfano sigue vivo y
        puede seguir mutando `self._adapter` después de que ya se le devolvió
        `ExecutionTimeoutError` al caller — `PaperAdapter` no es thread-safe
        (ver su docstring), así que en teoría podría competir con la llamada
        siguiente. Hoy es inofensivo porque `PaperAdapter` es 100% in-memory
        y no puede colgarse (nunca dispara este path). Cuando `BingXAdapter`
        (F16/F17) reuse este call site con I/O de red real, un hang real
        activaría este riesgo — la resolución de fondo en ese momento debería
        ser migrar `place_order` a un cliente async cancelable (`httpx`
        `AsyncClient` + `asyncio.wait_for`, como ya hace `bingx_fetcher.py`),
        no seguir extendiendo el wrapper de threads.
        """
        future = self._executor.submit(self._adapter.place_order, request)
        try:
            return future.result(timeout=self._timeout_seconds)
        except FutureTimeoutError as exc:
            self._executor.shutdown(wait=False)
            self._executor = ThreadPoolExecutor(max_workers=1)
            raise ExecutionTimeoutError(
                f"adapter.place_order() excedio timeout={self._timeout_seconds}s "
                f"(symbol={request.symbol}, client_order_id={request.client_order_id})"
            ) from exc

    def _persist_order(
        self, decision: ModelDecision, result: OrderResult, trade_id: str | None
    ) -> Order:
        order_row = Order(
            bot_run_id=self._bot_run_id,
            trade_id=trade_id,
            client_order_id=result.client_order_id,
            symbol=result.symbol,
            environment=self._environment.value,
            order_type=result.order_type.value,
            side=result.side.value,
            quantity=result.quantity_filled
            if result.status == OrderStatus.FILLED
            else result.quantity_requested,
            price=Decimal(str(decision.entry_price)),
            status=result.status.value,
            exchange_order_id=result.order_id,
            filled_at=result.timestamp_utc if result.status == OrderStatus.FILLED else None,
            fill_price=result.fill_price,
            fee=result.fee_usdt,
            is_simulated=result.is_simulated,
        )
        self._order_repo.save(order_row)
        return order_row

    def _persist_trade(
        self, decision: ModelDecision, adjusted: AdjustedParameters, result: OrderResult
    ) -> Trade:
        trade_row = Trade(
            bot_run_id=self._bot_run_id,
            symbol=decision.symbol,
            environment=self._environment.value,
            direction=decision.decision.value,
            entry_price=result.fill_price,
            margin_usdt=adjusted.margin_usdt,
            leverage=adjusted.leverage,
            quantity=result.quantity_filled,
            status="OPEN",
            opened_at=result.timestamp_utc,
        )
        self._session.add(trade_row)
        self._session.flush()
        return trade_row

    def _persist_position(
        self,
        decision: ModelDecision,
        adjusted: AdjustedParameters,
        result: OrderResult,
        trade_id: str,
    ) -> Position:
        position_row = Position(
            bot_run_id=self._bot_run_id,
            trade_id=trade_id,
            symbol=decision.symbol,
            environment=self._environment.value,
            direction=decision.decision.value,
            quantity=result.quantity_filled,
            entry_price=result.fill_price,
            margin_usdt=adjusted.margin_usdt,
            leverage=adjusted.leverage,
            stop_loss=Decimal(str(decision.stop_loss)),
            take_profit=Decimal(str(decision.take_profit)),
            status="OPEN",
            opened_at=result.timestamp_utc,
            updated_at=result.timestamp_utc,
        )
        self._session.add(position_row)
        self._session.flush()
        return position_row

    def _register_position_manager(self, decision: ModelDecision, atr_1h: Decimal) -> None:
        config = build_position_config(
            symbol=decision.symbol,
            stop_loss=Decimal(str(decision.stop_loss)),
            take_profit=Decimal(str(decision.take_profit)),
            use_trailing_stop=decision.position_management_plan.use_trailing_stop,
            defaults=self._position_management_defaults,
            atr_1h=atr_1h,
        )
        self._position_manager.set_config(config)


__all__ = ["ExecutionEngine", "ExecutionTimeoutError"]
