"""Señal breakout detection multi-timeframe (épica F5, tarjeta 49).

Detecta rupturas de rangos/niveles clave usando la vela 4h como referencia
de rango y confirma la señal con volumen relativo (tasa 5m vs baseline 1h/4h).

Algoritmo:
  1. Rango: box_top = 4h.high, box_bottom = 4h.low
  2. Desplazamiento: (last_price - nivel_roto) / nivel_roto  → % de ruptura
  3. Normalizar: tanh(desplazamiento / umbral)               → [-1, 1]
  4. Confirmación de volumen: vol_ratio = tasa_5m / baseline  → factor [0.2, 1.0]
  5. Señal final: breakout_raw * volume_factor               → [-1, 1]

Cuando el precio está dentro del rango, la señal se atenúa a ±0.1 máx para
reflejar posición relativa sin emitir una señal de ruptura falsa.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from backend.market_data.schemas import MarketSnapshot

# Un movimiento del 1 % fuera del rango → tanh(1) ≈ 0.76 (señal fuerte).
_BREAKOUT_THRESHOLD: Final[float] = 0.01

# Cuando el precio está dentro del rango, la señal vale como máximo ±0.1.
_INSIDE_ATTENUATION: Final[float] = 0.1

# Piso del factor de volumen: no anula la señal aunque el volumen sea bajo.
_MIN_VOL_FACTOR: Final[float] = 0.2

# Minutos por timeframe, para normalizar volumen a tasa por minuto.
_TF_MINUTES: Final[dict[str, int]] = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}

BREAKOUT_TIMEFRAMES: Final[tuple[str, ...]] = ("5m", "15m", "1h", "4h")


@dataclass(frozen=True)
class BreakoutResult:
    """Output del cálculo de señal breakout detection."""

    # Señal en [-1.0, 1.0]. Positivo = ruptura alcista, negativo = bajista.
    # Valores cercanos a 0 indican precio dentro del rango sin ruptura clara.
    signal: float
    rationale: dict[str, Any]  # desglose auditable


def _vol_rate(volume: Decimal, minutes: int) -> float:
    """Volumen normalizado a unidades por minuto."""
    return float(volume) / minutes


def calculate_breakout(snapshot: MarketSnapshot) -> BreakoutResult:
    """Calcula la señal de breakout detection a partir de un MarketSnapshot.

    Algoritmo:
      1. Define el rango de referencia con los extremos de la vela 4h.
      2. Si el precio actual rompe ese rango, calcula el % de ruptura
         normalizado con tanh (señal direccional en [-1, 1]).
      3. Confirma la señal multiplicando por un factor de volumen basado
         en la tasa de volumen 5m vs la baseline 1h/4h.
    """
    tf_4h = snapshot.candles.tf_4h
    tf_1h = snapshot.candles.tf_1h
    tf_5m = snapshot.candles.tf_5m

    box_top = float(tf_4h.high)
    box_bottom = float(tf_4h.low)
    last_price = float(snapshot.last_price)

    # --- Desplazamiento respecto al rango 4h ---
    breakout_pct: float | None
    if last_price > box_top:
        pct = (last_price - box_top) / box_top
        breakout_pct = pct
        raw_breakout = math.tanh(pct / _BREAKOUT_THRESHOLD)
        position = "above_range"
    elif last_price < box_bottom:
        pct = (last_price - box_bottom) / box_bottom  # negativo
        breakout_pct = pct
        raw_breakout = math.tanh(pct / _BREAKOUT_THRESHOLD)
        position = "below_range"
    else:
        breakout_pct = None
        mid = (box_top + box_bottom) / 2.0
        if mid == 0.0:
            raw_breakout = 0.0
        else:
            inside_pct = (last_price - mid) / mid
            raw_breakout = math.tanh(inside_pct / _BREAKOUT_THRESHOLD) * _INSIDE_ATTENUATION
        position = "inside_range"

    # --- Factor de confirmación por volumen ---
    vol_5m_rate = _vol_rate(tf_5m.volume, _TF_MINUTES["5m"])
    vol_1h_rate = _vol_rate(tf_1h.volume, _TF_MINUTES["1h"])
    vol_4h_rate = _vol_rate(tf_4h.volume, _TF_MINUTES["4h"])
    baseline_rate = (vol_1h_rate + vol_4h_rate) / 2.0

    if baseline_rate <= 0.0:
        vol_ratio = 1.0
        volume_factor = 1.0
    else:
        vol_ratio = vol_5m_rate / baseline_rate
        volume_factor = max(_MIN_VOL_FACTOR, min(1.0, vol_ratio))

    signal = max(-1.0, min(1.0, raw_breakout * volume_factor))

    rationale: dict[str, Any] = {
        "method": "4h_range_breakout_with_volume_confirmation",
        "range": {
            "box_top": box_top,
            "box_bottom": box_bottom,
            "last_price": last_price,
            "position": position,
            "breakout_pct": breakout_pct,
        },
        "volume": {
            "vol_5m_rate_per_min": vol_5m_rate,
            "vol_1h_rate_per_min": vol_1h_rate,
            "vol_4h_rate_per_min": vol_4h_rate,
            "baseline_rate_per_min": baseline_rate,
            "vol_ratio": vol_ratio,
            "volume_factor": volume_factor,
        },
        "raw_breakout": raw_breakout,
        "signal": signal,
    }

    return BreakoutResult(signal=signal, rationale=rationale)
