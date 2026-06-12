"""Lógica pura para mover el SL a break-even."""

from __future__ import annotations

from decimal import Decimal

from backend.exchange_adapters.schemas import OrderSide


def maybe_move_to_break_even(
    side: OrderSide,
    entry_price: Decimal,
    mark_price: Decimal,
    be_trigger_delta: Decimal,
    current_sl: Decimal | None,
) -> Decimal | None:
    """Retorna el nuevo SL si corresponde moverlo a break-even, o None si no cambia.

    El SL se mueve a entry_price cuando el precio se mueve be_trigger_delta a favor.
    Solo aplica si el SL actual está por debajo (LONG) o por encima (SHORT) de entry_price,
    evitando sobrescribir un SL ya favorable.

    Args:
        side: dirección de la posición
        entry_price: precio de entrada de la posición
        mark_price: precio actual de mercado
        be_trigger_delta: distancia mínima a favor necesaria para activar break-even
        current_sl: SL actual (None si no hay SL definido)

    Returns:
        entry_price si se debe mover a break-even, None si no hay cambio.
    """
    if side == OrderSide.BUY:
        trigger_price = entry_price + be_trigger_delta
        if mark_price < trigger_price:
            return None
        # Ya tiene SL por encima del entry (break-even o mejor) → no mover hacia atrás
        if current_sl is not None and current_sl >= entry_price:
            return None
        return entry_price
    else:
        trigger_price = entry_price - be_trigger_delta
        if mark_price > trigger_price:
            return None
        if current_sl is not None and current_sl <= entry_price:
            return None
        return entry_price
