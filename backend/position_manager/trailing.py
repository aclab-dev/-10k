"""Lógica pura del trailing stop."""

from __future__ import annotations

from decimal import Decimal

from backend.exchange_adapters.schemas import OrderSide


def compute_trailing_stop(
    side: OrderSide,
    mark_price: Decimal,
    trailing_delta: Decimal,
    high_water: Decimal | None,
) -> tuple[Decimal, Decimal]:
    """Actualiza el high-water mark y calcula el trailing stop price.

    Para LONG: high_water es el máximo precio visto. Trailing stop = high_water - trailing_delta.
    Para SHORT: high_water es el mínimo precio visto. Trailing stop = high_water + trailing_delta.

    Args:
        side: dirección de la posición
        mark_price: precio actual de mercado
        trailing_delta: distancia fija en unidades de precio
        high_water: valor previo del high-water mark (None en el primer tick)

    Returns:
        (new_high_water, trailing_stop_price)
    """
    if side == OrderSide.BUY:
        new_high_water = max(mark_price, high_water) if high_water is not None else mark_price
        trailing_stop = new_high_water - trailing_delta
    else:
        new_high_water = min(mark_price, high_water) if high_water is not None else mark_price
        trailing_stop = new_high_water + trailing_delta

    return new_high_water, trailing_stop


def is_trailing_stop_hit(side: OrderSide, mark_price: Decimal, trailing_stop: Decimal) -> bool:
    """Retorna True si el precio actual activó el trailing stop."""
    if side == OrderSide.BUY:
        return mark_price <= trailing_stop
    return mark_price >= trailing_stop
