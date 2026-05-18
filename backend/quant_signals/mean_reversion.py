"""Mean reversion signal — desviación normalizada respecto a media multi-timeframe.

Método: z-score de last_price respecto a la distribución de cierres de los 4
timeframes del MarketSnapshot, normalizado a [-1.0, 1.0] mediante tanh.

Convención de signo (igual que el resto de señales del paquete):
- Positivo (+): precio por debajo de la media → expectativa alcista (reversa alcista).
- Negativo (−): precio por encima de la media → expectativa bajista (reversa bajista).
"""

from __future__ import annotations

import math

from backend.market_data.schemas import MarketSnapshot

# Coeficiente de variación mínimo: evita señal en mercados completamente planos.
_MIN_CV = 1e-6


def compute_mean_reversion_signal(snapshot: MarketSnapshot) -> float:
    """Retorna la señal de mean reversion en [-1.0, 1.0].

    Toma los cierres de los 4 timeframes como proxy de historia reciente,
    calcula el z-score del precio actual respecto a esa distribución y lo
    normaliza con tanh. Devuelve 0.0 si el mercado está plano (std < _MIN_CV * mean).
    """
    closes = [
        float(snapshot.candles.tf_5m.close),
        float(snapshot.candles.tf_15m.close),
        float(snapshot.candles.tf_1h.close),
        float(snapshot.candles.tf_4h.close),
    ]
    n = len(closes)
    mean = sum(closes) / n
    variance = sum((c - mean) ** 2 for c in closes) / n
    std = math.sqrt(variance)

    if std < _MIN_CV * mean:
        return 0.0

    last_price = float(snapshot.last_price)
    z = (mean - last_price) / std
    return math.tanh(z)
