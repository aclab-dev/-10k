"""Señal de open interest (épica F5, tarea #50).

Mide el apalancamiento relativo comparando OI vs volumen del snapshot.
OI >> volumen → mercado sobre-apalancado → señal bajista contraria (riesgo de squeeze).
OI << volumen → mercado des-apalancado → señal alcista (capacidad para crecer).

Normalización: tanh aplicado al desvío del ratio (OI/volumen) respecto al neutro (1.0).
"""

from __future__ import annotations

import math
from decimal import Decimal

from backend.market_data.schemas import MarketSnapshot

# Ratio neutro: OI = volumen → señal = 0.
# Con _OI_SCALE=1.0: tanh((2.0-1.0)/1.0) ≈ -0.76 para OI=2×volumen;
# tanh satura cerca de ±1.0 cuando OI >> volumen (extremo sobre-apalancado) o
# OI ≈ 0 (extremo des-apalancado).
_OI_NEUTRAL_RATIO: float = 1.0
_OI_SCALE: float = 1.0


def compute_open_interest_signal(snapshot: MarketSnapshot) -> float:
    """Retorna la señal de open interest en [-1.0, 1.0].

    Devuelve 0.0 si el snapshot no incluye open_interest o si volume es cero.
    Señal negativa = OI alto relativo a volumen → mercado sobre-apalancado (bajista).
    Señal positiva = OI bajo relativo a volumen → mercado des-apalancado (alcista).
    """
    oi = snapshot.open_interest
    volume = snapshot.volume

    if oi is None or volume == Decimal("0"):
        return 0.0

    ratio = float(oi) / float(volume)
    return max(-1.0, min(1.0, -math.tanh((ratio - _OI_NEUTRAL_RATIO) / _OI_SCALE)))
