"""PositionManager — monitorea posiciones paper y ejecuta cierres por SL/TP/trailing.

Responsabilidades:
- Mantiene la configuración de salida (SL, TP, trailing, break-even) por símbolo.
- En cada tick(symbol, mark_price):
    1. Actualiza el high-water del trailing stop.
    2. Evalúa break-even y mueve el SL efectivo si corresponde.
    3. Evalúa SL efectivo → TP → trailing stop (primer trigger gana).
    4. Si hay trigger → coloca MARKET reduce-only y limpia la config.

Estado en memoria (sin persistencia). El caller es responsable de los ticks periódicos.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog

from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.position_manager.break_even import maybe_move_to_break_even
from backend.position_manager.schemas import (
    PositionConfig,
    PositionTriggerReason,
    TickResult,
)
from backend.position_manager.trailing import compute_trailing_stop, is_trailing_stop_hit

_log = structlog.get_logger(__name__)


class PositionManager:
    """Gestor de posiciones paper: monitorea SL, TP, trailing stop y break-even.

    Nota de thread safety: los dicts internos (_configs, _high_water, _trailing_stop,
    _effective_sl) se mutan en tick(). No es seguro llamar tick() concurrentemente
    para el mismo símbolo sin sincronización externa. El caller debe garantizar
    ejecución single-threaded o serializar el acceso por símbolo.
    """

    def __init__(self, adapter: ExchangeAdapter) -> None:
        self._adapter = adapter
        # Configuración de salida por símbolo
        self._configs: dict[str, PositionConfig] = {}
        # High-water mark para el trailing stop, por símbolo
        self._high_water: dict[str, Decimal] = {}
        # Trailing stop price actual (calculado en el último tick)
        self._trailing_stop: dict[str, Decimal] = {}
        # SL efectivo: puede diferir de config.stop_loss tras un movimiento a break-even
        self._effective_sl: dict[str, Decimal | None] = {}

    def set_config(self, config: PositionConfig) -> None:
        """Registra o reemplaza la configuración de salida para un símbolo."""
        self._configs[config.symbol] = config
        # Inicializar SL efectivo desde config; se actualizará si break-even se activa
        self._effective_sl[config.symbol] = config.stop_loss
        # Resetear estado de trailing al reconfigurar
        self._high_water.pop(config.symbol, None)
        self._trailing_stop.pop(config.symbol, None)
        _log.info(
            "position_manager.config_set",
            symbol=config.symbol,
            stop_loss=str(config.stop_loss),
            take_profit=str(config.take_profit),
            trailing_delta=str(config.trailing_delta),
            be_trigger_delta=str(config.be_trigger_delta),
        )

    def get_config(self, symbol: str) -> PositionConfig | None:
        return self._configs.get(symbol)

    def get_trailing_stop(self, symbol: str) -> Decimal | None:
        """Retorna el precio de trailing stop activo para `symbol`, o None si no aplica."""
        return self._trailing_stop.get(symbol)

    def get_effective_sl(self, symbol: str) -> Decimal | None:
        """Retorna el SL efectivo actual para `symbol` (puede haber sido movido a break-even)."""
        return self._effective_sl.get(symbol)

    def remove_config(self, symbol: str) -> None:
        """Elimina la configuración y el estado de tracking para un símbolo."""
        self._configs.pop(symbol, None)
        self._high_water.pop(symbol, None)
        self._trailing_stop.pop(symbol, None)
        self._effective_sl.pop(symbol, None)

    def tick(self, symbol: str, mark_price: Decimal) -> TickResult:
        """Evalúa triggers para `symbol` al precio `mark_price`.

        Orden de evaluación: SL efectivo → TP → trailing stop.
        El primero que se activa gana; se coloca la orden y se limpia la config.

        Si no hay posición abierta o no hay config → retorna NONE.

        Manejo de excepciones: si `place_order` lanza (timeout, error del adapter, etc.),
        la excepción se propaga al caller Y la config ya fue limpiada (via try/finally).
        Esto significa que la posición queda sin monitoreo activo. El caller debe capturar
        la excepción y llamar `set_config` de nuevo si quiere reintentar el cierre.
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

        # --- Break-even: mover SL efectivo a entry_price si se cumple la condición ---
        if config.be_trigger_delta is not None:
            current_sl = self._effective_sl.get(symbol)
            new_sl = maybe_move_to_break_even(
                side=side,
                entry_price=position.entry_price,
                mark_price=mark_price,
                be_trigger_delta=config.be_trigger_delta,
                current_sl=current_sl,
            )
            if new_sl is not None:
                self._effective_sl[symbol] = new_sl
                _log.info(
                    "position_manager.break_even_activated",
                    symbol=symbol,
                    entry_price=str(position.entry_price),
                    mark_price=str(mark_price),
                    new_sl=str(new_sl),
                )

        # --- Evaluación de triggers ---
        effective_sl = self._effective_sl.get(symbol)
        trigger = self._evaluate_trigger(
            side, mark_price, config, effective_sl, trailing_stop_price
        )

        if trigger == PositionTriggerReason.NONE:
            return TickResult(
                symbol=symbol,
                trigger=PositionTriggerReason.NONE,
                mark_price=mark_price,
            )

        # remove_config en finally: si place_order lanza, la config queda limpia y el
        # siguiente tick no intentará cerrar de nuevo (evita doble orden al migrar a live).
        try:
            close_order_id = self._place_close_order(symbol, position.quantity, mark_price, side)
        finally:
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
        effective_sl: Decimal | None,
        trailing_stop_price: Decimal | None,
    ) -> PositionTriggerReason:
        # SL efectivo (puede ser el original o el movido a break-even)
        if effective_sl is not None:
            if side == OrderSide.BUY and mark_price <= effective_sl:
                return PositionTriggerReason.SL_HIT
            if side == OrderSide.SELL and mark_price >= effective_sl:
                return PositionTriggerReason.SL_HIT

        # TP
        if config.take_profit is not None:
            if side == OrderSide.BUY and mark_price >= config.take_profit:
                return PositionTriggerReason.TP_HIT
            if side == OrderSide.SELL and mark_price <= config.take_profit:
                return PositionTriggerReason.TP_HIT

        # Trailing stop
        if trailing_stop_price is not None:
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
            # price en MARKET es intencional para PAPER: PaperAdapter lo usa como precio
            # de referencia para simular el fill + slippage. Los adapters de exchange real
            # (BingX, Binance) DEBEN ignorar explícitamente este campo en órdenes MARKET
            # y nunca enviarlo a la API — de lo contrario podría crear una orden limitada.
            price=mark_price,
            is_reduce_only=True,
        )
        self._adapter.place_order(request)
        return client_order_id
