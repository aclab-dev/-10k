"""Lógica pura del trailing stop.

Soporta tres modos de distancia de trailing (F14), resueltos por `resolve_trailing_delta`:
- FIXED: distancia absoluta fija en unidades de precio.
- PERCENT: fracción del high-water mark (crece con el precio a favor).
- ATR: ATR (valor absoluto) multiplicado por un factor.

En todos los casos la distancia resuelta se aplica igual: high_water − delta (LONG) o
high_water + delta (SHORT).
"""

from __future__ import annotations

from decimal import Decimal

from backend.exchange_adapters.schemas import OrderSide
from backend.position_manager.schemas import TrailingMode


def update_high_water(
    side: OrderSide,
    mark_price: Decimal,
    high_water: Decimal | None,
) -> Decimal:
    """Actualiza el high-water mark según el lado de la posición.

    LONG: máximo precio visto. SHORT: mínimo precio visto. En el primer tick
    (high_water is None) se inicializa con mark_price.
    """
    if high_water is None:
        return mark_price
    if side == OrderSide.BUY:
        return max(mark_price, high_water)
    return min(mark_price, high_water)


def trailing_stop_from_delta(side: OrderSide, high_water: Decimal, delta: Decimal) -> Decimal:
    """Precio de trailing stop dado un high-water y una distancia absoluta `delta`."""
    if side == OrderSide.BUY:
        return high_water - delta
    return high_water + delta


def resolve_trailing_delta(
    mode: TrailingMode,
    reference_price: Decimal,
    *,
    fixed_delta: Decimal | None = None,
    percent: Decimal | None = None,
    atr_value: Decimal | None = None,
    atr_multiplier: Decimal | None = None,
) -> Decimal:
    """Resuelve la distancia absoluta de trailing según el modo.

    Args:
        mode: modo de trailing (FIXED, PERCENT, ATR).
        reference_price: precio de referencia para PERCENT (típicamente el high-water).
        fixed_delta: distancia absoluta (requerido en FIXED).
        percent: fracción a aplicar sobre reference_price (requerido en PERCENT).
        atr_value: valor ATR absoluto (requerido en ATR).
        atr_multiplier: factor sobre el ATR (default 1 si no se provee).

    Returns:
        Distancia absoluta en unidades de precio (siempre > 0 con inputs válidos).
    """
    if mode == TrailingMode.FIXED:
        if fixed_delta is None:
            raise ValueError("FIXED trailing requires fixed_delta.")
        return fixed_delta
    if mode == TrailingMode.PERCENT:
        if percent is None:
            raise ValueError("PERCENT trailing requires percent.")
        return reference_price * percent
    if mode == TrailingMode.ATR:
        if atr_value is None:
            raise ValueError("ATR trailing requires atr_value.")
        multiplier = atr_multiplier if atr_multiplier is not None else Decimal("1")
        return atr_value * multiplier
    raise ValueError(f"Unknown trailing mode: {mode!r}")


def compute_trailing_stop(
    side: OrderSide,
    mark_price: Decimal,
    trailing_delta: Decimal,
    high_water: Decimal | None,
) -> tuple[Decimal, Decimal]:
    """Actualiza el high-water mark y calcula el trailing stop con distancia fija.

    Conveniencia para el modo FIXED. Para LONG: trailing stop = high_water − trailing_delta.
    Para SHORT: trailing stop = high_water + trailing_delta.

    Args:
        side: dirección de la posición
        mark_price: precio actual de mercado
        trailing_delta: distancia fija en unidades de precio
        high_water: valor previo del high-water mark (None en el primer tick)

    Returns:
        (new_high_water, trailing_stop_price)
    """
    new_high_water = update_high_water(side, mark_price, high_water)
    return new_high_water, trailing_stop_from_delta(side, new_high_water, trailing_delta)


def is_trailing_stop_hit(side: OrderSide, mark_price: Decimal, trailing_stop: Decimal) -> bool:
    """Retorna True si el precio actual activó el trailing stop."""
    if side == OrderSide.BUY:
        return mark_price <= trailing_stop
    return mark_price >= trailing_stop
