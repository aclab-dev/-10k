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
    be_sl_offset: Decimal = Decimal("0"),
) -> Decimal | None:
    """Retorna el nuevo SL si corresponde moverlo a break-even, o None si no cambia.

    El SL se mueve a entry_price ± be_sl_offset cuando el precio se mueve
    be_trigger_delta a favor. be_sl_offset=0 (default) mueve exactamente a entry_price;
    un valor positivo ubica el SL be_sl_offset por encima de entry (LONG) o por
    debajo (SHORT), cubriendo fees u otro buffer configurable.

    Solo aplica si el SL actual está por debajo del target (LONG) o por encima (SHORT),
    evitando sobrescribir un SL ya más favorable.

    Args:
        side: dirección de la posición
        entry_price: precio de entrada de la posición
        mark_price: precio actual de mercado
        be_trigger_delta: distancia mínima a favor necesaria para activar break-even
        current_sl: SL actual (None si no hay SL definido)
        be_sl_offset: desplazamiento sobre entry_price donde queda el SL (ge=0)

    Returns:
        nuevo SL si se debe mover, None si no hay cambio.
    """
    if side == OrderSide.BUY:
        trigger_price = entry_price + be_trigger_delta
        if mark_price < trigger_price:
            return None
        be_sl = entry_price + be_sl_offset
        if current_sl is not None and current_sl >= be_sl:
            return None
        return be_sl
    else:
        trigger_price = entry_price - be_trigger_delta
        if mark_price > trigger_price:
            return None
        be_sl = entry_price - be_sl_offset
        if current_sl is not None and current_sl <= be_sl:
            return None
        return be_sl
