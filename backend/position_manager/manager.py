"""PositionManager — monitorea posiciones paper y ejecuta cierres por SL/TP/trailing.

Responsabilidades:
- Mantiene la configuración de salida (SL, TP, trailing) por símbolo.
- En cada tick(symbol, mark_price) evalúa si algún trigger fue alcanzado.
- Si hay trigger → coloca una orden MARKET reduce-only via el ExchangeAdapter.

No persiste estado en DB. El estado del trailing (high-water) vive en memoria.
El caller es responsable de llamar tick() con mark prices actualizados.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog

from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.position_manager.schemas import (
    PositionConfig,
    PositionTriggerReason,
    TickResult,
)
from backend.position_manager.trailing import compute_trailing_stop, is_trailing_stop_hit

_log = structlog.get_logger(__name__)


class PositionManager:
    """Gestor de posiciones paper: monitorea SL, TP y trailing stop."""

    def __init__(self, adapter: ExchangeAdapter) -> None:
        self._adapter = adapter
        # Configuración de salida por símbolo
        self._configs: dict[str, PositionConfig] = {}
        # High-water mark para el trailing stop, por símbolo
        self._high_water: dict[str, Decimal] = {}
        # Trailing stop price actual por símbolo (calculado en el último tick)
        self._trailing_stop: dict[str, Decimal] = {}

    def set_config(self, config: PositionConfig) -> None:
        """Registra o reemplaza la configuración de salida para un símbolo."""
        self._configs[config.symbol] = config
        # Resetear estado de trailing al reconfigurar
        self._high_water.pop(config.symbol, None)
        self._trailing_stop.pop(config.symbol, None)
        _log.info(
            "position_manager.config_set",
            symbol=config.symbol,
            stop_loss=str(config.stop_loss),
            take_profit=str(config.take_profit),
            trailing_delta=str(config.trailing_delta),
        )

    def get_config(self, symbol: str) -> PositionConfig | None:
        return self._configs.get(symbol)

    def remove_config(self, symbol: str) -> None:
        """Elimina la configuración y el estado de trailing para un símbolo."""
        self._configs.pop(symbol, None)
        self._high_water.pop(symbol, None)
        self._trailing_stop.pop(symbol, None)

    def tick(self, symbol: str, mark_price: Decimal) -> TickResult:
        """Evalúa triggers para `symbol` al precio `mark_price`.

        Orden de evaluación: SL estático → TP → trailing stop.
        El primero que se activa gana; se coloca la orden y se limpia la config.

        Si no hay posición abierta o no hay config → retorna NONE.
        """
        position = self._adapter.get_position(symbol)
        if position is None:
            return TickResult(
                symbol=symbol,
                trigger=PositionTriggerReason.NONE,
                mark_price=mark_price,
            )

        config = self._configs.get(symbol)
        if config is None:
            return TickResult(
                symbol=symbol,
                trigger=PositionTriggerReason.NONE,
                mark_price=mark_price,
            )

        side = position.side

        # --- Trailing stop: actualizar high-water y precio de trailing ---
        trailing_stop_price: Decimal | None = None
        if config.trailing_delta is not None:
            hw, trailing_stop_price = compute_trailing_stop(
                side=side,
                mark_price=mark_price,
                trailing_delta=config.trailing_delta,
                high_water=self._high_water.get(symbol),
            )
            self._high_water[symbol] = hw
            self._trailing_stop[symbol] = trailing_stop_price

        # --- Evaluación de triggers ---
        trigger = self._evaluate_trigger(side, mark_price, config, trailing_stop_price)

        if trigger == PositionTriggerReason.NONE:
            return TickResult(
                symbol=symbol,
                trigger=PositionTriggerReason.NONE,
                mark_price=mark_price,
            )

        # Disparar cierre
        close_order_id = self._place_close_order(symbol, position.quantity, mark_price, side)
        self.remove_config(symbol)

        _log.info(
            "position_manager.trigger_fired",
            symbol=symbol,
            trigger=trigger,
            mark_price=str(mark_price),
            close_order_id=close_order_id,
        )

        return TickResult(
            symbol=symbol,
            trigger=trigger,
            mark_price=mark_price,
            close_order_id=close_order_id,
        )

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _evaluate_trigger(
        self,
        side: OrderSide,
        mark_price: Decimal,
        config: PositionConfig,
        trailing_stop_price: Decimal | None,
    ) -> PositionTriggerReason:
        # SL estático
        if config.stop_loss is not None:
            if side == OrderSide.BUY and mark_price <= config.stop_loss:
                return PositionTriggerReason.SL_HIT
            if side == OrderSide.SELL and mark_price >= config.stop_loss:
                return PositionTriggerReason.SL_HIT

        # TP
        if config.take_profit is not None:
            if side == OrderSide.BUY and mark_price >= config.take_profit:
                return PositionTriggerReason.TP_HIT
            if side == OrderSide.SELL and mark_price <= config.take_profit:
                return PositionTriggerReason.TP_HIT

        # Trailing stop
        if trailing_stop_price is not None and config.trailing_delta is not None:
            if is_trailing_stop_hit(side, mark_price, trailing_stop_price):
                return PositionTriggerReason.TRAILING_SL_HIT

        return PositionTriggerReason.NONE

    def _place_close_order(
        self,
        symbol: str,
        quantity: Decimal,
        mark_price: Decimal,
        side: OrderSide,
    ) -> str:
        close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        client_order_id = str(uuid.uuid4())

        request = OrderRequest(
            client_order_id=client_order_id,
            symbol=symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            price=mark_price,
            is_reduce_only=True,
        )
        self._adapter.place_order(request)
        return client_order_id
