"""Modelo de funding para futuros perpetuos.

En futuros perpetuos, el funding se aplica cada 8 horas.
Si el rate es positivo, los longs pagan a los shorts.
Si el rate es negativo, los shorts pagan a los longs.
El monto = notional × funding_rate.
"""

from __future__ import annotations

from decimal import Decimal

from backend.exchange_adapters.schemas import OrderSide


def compute_funding_payment(
    side: OrderSide,
    quantity: Decimal,
    mark_price: Decimal,
    funding_rate: Decimal,
) -> Decimal:
    """Calcula el pago de funding para una posición.

    Retorna:
        Decimal positivo → nosotros pagamos (sale de nuestro balance).
        Decimal negativo → nosotros recibimos (entra a nuestro balance).
    """
    notional = quantity * mark_price
    if side == OrderSide.BUY:
        return notional * funding_rate
    return -(notional * funding_rate)
