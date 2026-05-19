"""Señal de funding rate (épica F5, tarea #50).

Funding positivo → longs sobre-representados (crowd bullish) → señal bajista.
Funding negativo → shorts sobre-representados (crowd bearish) → señal alcista.

Normalización: tanh con _FUNDING_SCALE calibrado para detectar extremos en crypto.
"""

from __future__ import annotations

import math

from backend.market_data.schemas import MarketSnapshot

# Funding típico de BTC ≈ 0.01% (0.0001); extremo ≈ 0.03–0.10% (0.0003–0.001).
# Con esta escala: tanh(0.0003 / 0.0003) ≈ 0.76 (señal fuerte a 0.03%);
# tanh satura cerca de ±1.0 en eventos extremos de funding.
_FUNDING_SCALE: float = 3e-4


def compute_funding_signal(snapshot: MarketSnapshot) -> float:
    """Retorna la señal de funding rate en [-1.0, 1.0].

    Devuelve 0.0 si el snapshot no incluye funding_rate.
    Señal negativa = funding positivo (longs apalancados) → expectativa bajista.
    Señal positiva = funding negativo (shorts apalancados) → expectativa alcista.
    """
    rate = snapshot.funding_rate
    if rate is None:
        return 0.0
    return max(-1.0, min(1.0, -math.tanh(rate / _FUNDING_SCALE)))
