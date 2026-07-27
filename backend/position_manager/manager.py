"""PositionManager — monitorea posiciones paper y ejecuta cierres por SL/TP/trailing.

Responsabilidades:
- Mantiene la configuración de salida (SL, TP, trailing, break-even) por símbolo.
- En cada tick(symbol, mark_price):
    1. Actualiza el high-water del trailing stop.
    2. Evalúa break-even y mueve el SL efectivo si corresponde.
    3. Evalúa (por prioridad): SL efectivo → TP (single o multi) → trailing stop.
    4. Si hay trigger → coloca MARKET reduce-only y limpia la config.

Gestión SL/TP avanzada (F14):
- update_sl / update_tp: actualización dinámica sin resetear trailing ni BE.
- take_profit_levels: multi-TP con cierre parcial en cada nivel.
- trigger_setup_invalidation: aplica la InvalidationAction configurada.

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
    TakeProfitLevel,
    TickResult,
    TrailingMode,
)
from backend.position_manager.trailing import (
    is_trailing_stop_hit,
    resolve_trailing_delta,
    trailing_stop_from_delta,
    update_high_water,
)

_log = structlog.get_logger(__name__)


class PositionManager:
    """Gestor de posiciones paper: monitorea SL, TP, trailing stop y break-even.

    Nota de thread safety: los dicts internos se mutan en tick(). No es seguro llamar
    tick() concurrentemente para el mismo símbolo sin sincronización externa.
    """

    def __init__(self, adapter: ExchangeAdapter) -> None:
        self._adapter = adapter
        self._configs: dict[str, PositionConfig] = {}
        # High-water mark para el trailing stop
        self._high_water: dict[str, Decimal] = {}
        # Trailing stop price actual
        self._trailing_stop: dict[str, Decimal] = {}
        # SL efectivo: puede diferir de config.stop_loss tras break-even o update_sl()
        self._effective_sl: dict[str, Decimal | None] = {}
        # TP efectivo para single-TP dinámico (puede diferir de config.take_profit)
        self._effective_tp: dict[str, Decimal | None] = {}
        # Niveles de multi-TP pendientes de dispararse (copia mutable de config)
        self._remaining_tp_levels: dict[str, list[TakeProfitLevel]] = {}
        # ATR suavizado (EMA) para trailing_atr_dynamic
        self._smoothed_atr: dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # Configuración
    # ------------------------------------------------------------------

    def set_config(self, config: PositionConfig) -> None:
        """Registra o reemplaza la configuración de salida para un símbolo."""
        self._configs[config.symbol] = config
        self._effective_sl[config.symbol] = config.stop_loss
        self._effective_tp[config.symbol] = config.take_profit
        self._remaining_tp_levels[config.symbol] = list(config.take_profit_levels)
        self._high_water.pop(config.symbol, None)
        self._trailing_stop.pop(config.symbol, None)
        self._smoothed_atr.pop(config.symbol, None)
        _log.info(
            "position_manager.config_set",
            symbol=config.symbol,
            stop_loss=str(config.stop_loss),
            take_profit=str(config.take_profit),
            tp_levels=len(config.take_profit_levels),
            trailing_mode=config.resolved_trailing_mode,
            trailing_delta=str(config.trailing_delta),
            trailing_percent=str(config.trailing_percent),
            trailing_atr=str(config.trailing_atr),
            trailing_atr_multiplier=str(config.trailing_atr_multiplier),
            trailing_atr_dynamic=config.trailing_atr_dynamic,
            trailing_atr_smoothing_alpha=str(config.trailing_atr_smoothing_alpha),
            be_trigger_delta=str(config.be_trigger_delta),
        )

    def get_config(self, symbol: str) -> PositionConfig | None:
        return self._configs.get(symbol)

    def get_trailing_stop(self, symbol: str) -> Decimal | None:
        return self._trailing_stop.get(symbol)

    def get_effective_sl(self, symbol: str) -> Decimal | None:
        return self._effective_sl.get(symbol)

    def get_effective_tp(self, symbol: str) -> Decimal | None:
        return self._effective_tp.get(symbol)

    def get_remaining_tp_levels(self, symbol: str) -> list[TakeProfitLevel]:
        return list(self._remaining_tp_levels.get(symbol, []))

    def remove_config(self, symbol: str) -> None:
        """Elimina la configuración y el estado de tracking para un símbolo."""
        self._configs.pop(symbol, None)
        self._high_water.pop(symbol, None)
        self._trailing_stop.pop(symbol, None)
        self._smoothed_atr.pop(symbol, None)
        self._effective_sl.pop(symbol, None)
        self._effective_tp.pop(symbol, None)
        self._remaining_tp_levels.pop(symbol, None)

    # ------------------------------------------------------------------
    # Actualización dinámica de SL/TP (F14)
    # ------------------------------------------------------------------

    def update_sl(self, symbol: str, new_sl: Decimal) -> None:
        """Actualiza el SL efectivo sin resetear trailing ni break-even."""
        if symbol not in self._configs:
            raise KeyError(f"No active config for symbol {symbol!r}")
        self._effective_sl[symbol] = new_sl
        _log.info("position_manager.sl_updated", symbol=symbol, new_sl=str(new_sl))

    def update_tp(self, symbol: str, new_tp: Decimal) -> None:
        """Actualiza el TP efectivo (single-TP mode).

        Si hay niveles multi-TP activos, los descarta y pasa a single-TP con new_tp.
        """
        if symbol not in self._configs:
            raise KeyError(f"No active config for symbol {symbol!r}")
        n_discarded = len(self._remaining_tp_levels.get(symbol, []))
        self._remaining_tp_levels[symbol] = []
        self._effective_tp[symbol] = new_tp
        _log.info(
            "position_manager.tp_updated",
            symbol=symbol,
            new_tp=str(new_tp),
            n_levels_discarded=n_discarded,
        )

    # ------------------------------------------------------------------
    # Invalidación de setup (F14)
    # ------------------------------------------------------------------

    def trigger_setup_invalidation(self, symbol: str, mark_price: Decimal) -> TickResult | None:
        """Aplica la InvalidationAction configurada para el símbolo.

        Retorna TickResult con trigger=SETUP_INVALIDATED si se tomó alguna acción,
        o None si no hay config, no hay action, o no hay posición abierta.
        """
        config = self._configs.get(symbol)
        if config is None or config.invalidation_action is None:
            return None

        position = self._adapter.get_position(symbol)
        if position is None:
            return None

        action = config.invalidation_action
        close_order_id: str | None = None
        closed_fraction: Decimal | None = None

        if action.close_fraction > Decimal("0"):
            close_qty = position.quantity * action.close_fraction
            is_full_close = action.close_fraction >= Decimal("1")
            try:
                close_order_id = self._place_close_order(
                    symbol, close_qty, mark_price, position.side
                )
            finally:
                if is_full_close:
                    self.remove_config(symbol)
            closed_fraction = action.close_fraction

        # Aplicar nuevo SL después de confirmar la orden (o si no hay orden).
        # Si el config fue eliminado por full-close, la actualización de SL es innecesaria.
        if action.new_sl is not None and symbol in self._configs:
            self._effective_sl[symbol] = action.new_sl
            _log.info(
                "position_manager.invalidation_sl_moved",
                symbol=symbol,
                new_sl=str(action.new_sl),
            )

        _log.info(
            "position_manager.setup_invalidated",
            symbol=symbol,
            close_order_id=close_order_id,
            closed_fraction=str(closed_fraction) if closed_fraction is not None else None,
            new_sl=str(action.new_sl) if action.new_sl is not None else None,
        )

        return TickResult(
            symbol=symbol,
            trigger=PositionTriggerReason.SETUP_INVALIDATED,
            mark_price=mark_price,
            close_order_id=close_order_id,
            closed_fraction=closed_fraction,
        )

    # ------------------------------------------------------------------
    # Tick principal
    # ------------------------------------------------------------------

    def tick(self, symbol: str, mark_price: Decimal, *, atr: Decimal | None = None) -> TickResult:
        """Evalúa triggers para `symbol` al precio `mark_price`.

        Orden de evaluación: SL efectivo → TP (single o multi) → trailing stop.
        El primero que se activa gana.

        Para multi-TP: en cada nivel disparado se hace un cierre parcial y se
        continúa el monitoreo (config no se elimina hasta que no queden niveles o
        se dispare SL/trailing). TickResult.trigger es TP_PARTIAL si aún quedan
        niveles, TP_HIT si fue el último.

        Manejo de excepciones — full-close (SL, TP_HIT single, último nivel multi-TP,
        trailing): config limpiada via try/finally; el caller debe capturar y llamar
        set_config para reintentar. Nivel parcial de multi-TP: si place_order lanza,
        el nivel NO se consume; el próximo tick lo reintenta automáticamente.
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
        trailing_mode = config.resolved_trailing_mode
        if trailing_mode is not None:
            hw = update_high_water(side, mark_price, self._high_water.get(symbol))
            # reference_price = high-water: para PERCENT la distancia se recalcula por
            # tick sobre el high-water (crece con el precio a favor).
            atr_value = config.trailing_atr
            if trailing_mode == TrailingMode.ATR and config.trailing_atr_dynamic:
                if atr is not None and atr <= 0:
                    _log.warning("position_manager.atr_invalid_value", symbol=symbol, atr=atr)
                    atr = None
                atr_value = self._update_smoothed_atr(symbol, config, atr)
            self._high_water[symbol] = hw
            # ATR dynamic with no value yet (no seed, feed not yet available): skip
            # trailing computation this tick to avoid crashing resolve_trailing_delta.
            if atr_value is None and trailing_mode == TrailingMode.ATR:
                _log.warning("position_manager.atr_feed_unavailable", symbol=symbol)
            else:
                delta = resolve_trailing_delta(
                    trailing_mode,
                    reference_price=hw,
                    fixed_delta=config.trailing_delta,
                    percent=config.trailing_percent,
                    atr_value=atr_value,
                    atr_multiplier=config.trailing_atr_multiplier,
                )
                trailing_stop_price = trailing_stop_from_delta(side, hw, delta)
                self._trailing_stop[symbol] = trailing_stop_price

        # --- Break-even: mover SL efectivo a entry_price si corresponde ---
        if config.be_trigger_delta is not None:
            current_sl = self._effective_sl.get(symbol)
            new_sl = maybe_move_to_break_even(
                side=side,
                entry_price=position.entry_price,
                mark_price=mark_price,
                be_trigger_delta=config.be_trigger_delta,
                current_sl=current_sl,
                be_sl_offset=config.be_sl_offset,
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

        # --- 1. SL efectivo (mayor prioridad) ---
        effective_sl = self._effective_sl.get(symbol)
        if effective_sl is not None:
            sl_hit = (side == OrderSide.BUY and mark_price <= effective_sl) or (
                side == OrderSide.SELL and mark_price >= effective_sl
            )
            if sl_hit:
                try:
                    order_id = self._place_close_order(symbol, position.quantity, mark_price, side)
                finally:
                    self.remove_config(symbol)
                _log.info(
                    "position_manager.trigger_fired",
                    symbol=symbol,
                    trigger=PositionTriggerReason.SL_HIT,
                    mark_price=str(mark_price),
                    close_order_id=order_id,
                )
                return TickResult(
                    symbol=symbol,
                    trigger=PositionTriggerReason.SL_HIT,
                    mark_price=mark_price,
                    close_order_id=order_id,
                )

        # --- 2. TP: multi-TP o single TP ---
        remaining_levels = self._remaining_tp_levels.get(symbol, [])
        if remaining_levels:
            next_level = remaining_levels[0]
            # Nota: si el precio salta varios niveles en un mismo tick, solo se
            # dispara el primero; los demás se evalúan en ticks posteriores.
            tp_hit = (side == OrderSide.BUY and mark_price >= next_level.price) or (
                side == OrderSide.SELL and mark_price <= next_level.price
            )
            if tp_hit:
                tp_idx = len(config.take_profit_levels) - len(remaining_levels)
                # TODO: close_qty puede quedar fuera del step size del par.
                # PaperAdapter no cuantiza; el ExchangeAdapter real debe hacerlo
                # antes de enviar la orden al exchange (riesgo de rechazo en live).
                close_qty = position.quantity * next_level.close_fraction
                is_last_level = len(remaining_levels) == 1
                # Para el último nivel (full-close): remove_config en finally garantiza
                # limpieza aunque place_order lance, consistente con SL_HIT/TP_HIT.
                # Para niveles parciales: pop(0) va FUERA del finally — si la orden
                # falla, el nivel se preserva y el próximo tick puede reintentar.
                try:
                    order_id = self._place_close_order(symbol, close_qty, mark_price, side)
                finally:
                    if is_last_level:
                        self.remove_config(symbol)
                remaining_levels.pop(0)
                trigger = (
                    PositionTriggerReason.TP_HIT
                    if is_last_level
                    else PositionTriggerReason.TP_PARTIAL
                )
                _log.info(
                    "position_manager.trigger_fired",
                    symbol=symbol,
                    trigger=trigger,
                    tp_level_index=tp_idx,
                    mark_price=str(mark_price),
                    close_order_id=order_id,
                    closed_fraction=str(next_level.close_fraction),
                )
                return TickResult(
                    symbol=symbol,
                    trigger=trigger,
                    mark_price=mark_price,
                    close_order_id=order_id,
                    tp_level_index=tp_idx,
                    closed_fraction=next_level.close_fraction,
                )
        else:
            # Single TP: usar _effective_tp (puede haber sido actualizado dinámicamente)
            effective_tp = self._effective_tp.get(symbol)
            if effective_tp is not None:
                tp_hit = (side == OrderSide.BUY and mark_price >= effective_tp) or (
                    side == OrderSide.SELL and mark_price <= effective_tp
                )
                if tp_hit:
                    try:
                        order_id = self._place_close_order(
                            symbol, position.quantity, mark_price, side
                        )
                    finally:
                        self.remove_config(symbol)
                    _log.info(
                        "position_manager.trigger_fired",
                        symbol=symbol,
                        trigger=PositionTriggerReason.TP_HIT,
                        mark_price=str(mark_price),
                        close_order_id=order_id,
                    )
                    return TickResult(
                        symbol=symbol,
                        trigger=PositionTriggerReason.TP_HIT,
                        mark_price=mark_price,
                        close_order_id=order_id,
                    )

        # --- 3. Trailing stop ---
        if trailing_stop_price is not None and is_trailing_stop_hit(
            side, mark_price, trailing_stop_price
        ):
            try:
                order_id = self._place_close_order(symbol, position.quantity, mark_price, side)
            finally:
                self.remove_config(symbol)
            _log.info(
                "position_manager.trigger_fired",
                symbol=symbol,
                trigger=PositionTriggerReason.TRAILING_SL_HIT,
                mark_price=str(mark_price),
                close_order_id=order_id,
            )
            return TickResult(
                symbol=symbol,
                trigger=PositionTriggerReason.TRAILING_SL_HIT,
                mark_price=mark_price,
                close_order_id=order_id,
            )

        return TickResult(
            symbol=symbol,
            trigger=PositionTriggerReason.NONE,
            mark_price=mark_price,
        )

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _update_smoothed_atr(
        self,
        symbol: str,
        config: PositionConfig,
        atr: Decimal | None,
    ) -> Decimal | None:
        """Applies EMA smoothing to the live ATR and returns the value for delta resolution.

        When atr=None (feed momentarily unavailable), returns the last smoothed value or
        the seed from config without updating state.
        """
        prev = self._smoothed_atr.get(symbol, config.trailing_atr)
        if atr is None:
            return prev
        alpha = (
            config.trailing_atr_smoothing_alpha
            if config.trailing_atr_smoothing_alpha is not None
            else Decimal("1")
        )
        # On the seed tick (prev is None), skip EMA and use raw ATR directly.
        smoothed = alpha * atr + (Decimal("1") - alpha) * prev if prev is not None else atr
        self._smoothed_atr[symbol] = smoothed
        return smoothed

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
