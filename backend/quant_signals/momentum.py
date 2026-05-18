"""Señal momentum multi-timeframe (épica F5).

Calcula la señal de momentum basada en ROC intra-vela por timeframe:
  roc = (close - open) / open

Nota: este ROC mide el movimiento dentro de la vela actual, no el ROC clásico
entre cierres de velas consecutivas. Se normaliza via tanh con umbrales
calibrados por TF y se agrega con pesos que priorizan los TFs más largos.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from backend.market_data.schemas import CandleData, MarketSnapshot

# Pesos por timeframe (suma = 1.0). Mayor peso a TFs más largos.
_TF_WEIGHTS: Final[dict[str, float]] = {
    "5m": 0.10,
    "15m": 0.20,
    "1h": 0.30,
    "4h": 0.40,
}

# Umbral de ROC por TF ≈ movimiento "1-sigma" esperado en crypto futures.
# tanh(roc / threshold) mapea a ~0.76 cuando roc == threshold (señal fuerte).
_TF_ROC_THRESHOLD: Final[dict[str, float]] = {
    "5m": 0.002,  # 0.2%
    "15m": 0.004,  # 0.4%
    "1h": 0.010,  # 1.0%
    "4h": 0.020,  # 2.0%
}

MOMENTUM_TIMEFRAMES: Final[tuple[str, ...]] = ("5m", "15m", "1h", "4h")


@dataclass(frozen=True)
class MomentumResult:
    """Output del cálculo de señal momentum."""

    # Señal en [-1.0, 1.0] basada en ROC intra-vela (close-open)/open de cada TF.
    # Negativo = bearish, positivo = bullish. No refleja momentum entre velas
    # consecutivas: si el precio subió ayer y hoy abre flat, esta señal vale 0.
    signal: float
    rationale: dict[str, Any]  # desglose auditable por timeframe


def _roc(candle: CandleData) -> float:
    """Rate of Change intra-vela: (close - open) / open."""
    if candle.open == Decimal("0"):
        return 0.0
    return float((candle.close - candle.open) / candle.open)


def calculate_momentum(snapshot: MarketSnapshot) -> MomentumResult:
    """Calcula la señal de momentum multi-timeframe a partir de un MarketSnapshot.

    Algoritmo:
      1. Por cada TF: roc = (close - open) / open
      2. Normalizar: norm = tanh(roc / threshold_tf)  → [-1, 1]
      3. Agregar: signal = Σ weight_tf * norm_tf       → [-1, 1]

    El resultado es reproducible y determinístico: dada la misma entrada,
    siempre produce la misma salida.
    """
    candles_by_tf: dict[str, CandleData] = {
        "5m": snapshot.candles.tf_5m,
        "15m": snapshot.candles.tf_15m,
        "1h": snapshot.candles.tf_1h,
        "4h": snapshot.candles.tf_4h,
    }

    per_tf_roc: dict[str, float] = {}
    per_tf_normalized: dict[str, float] = {}

    for tf in MOMENTUM_TIMEFRAMES:
        roc = _roc(candles_by_tf[tf])
        per_tf_roc[tf] = roc
        per_tf_normalized[tf] = math.tanh(roc / _TF_ROC_THRESHOLD[tf])

    raw_signal = sum(_TF_WEIGHTS[tf] * per_tf_normalized[tf] for tf in MOMENTUM_TIMEFRAMES)
    # Clip defensivo: la suma ponderada está teóricamente en [-1, 1] pero
    # la aritmética float puede producir valores marginalmente fuera del rango.
    signal = max(-1.0, min(1.0, raw_signal))

    rationale: dict[str, Any] = {
        "method": "weighted_tanh_roc",
        "timeframes": {
            tf: {
                "roc": per_tf_roc[tf],
                "normalized": per_tf_normalized[tf],
                "weight": _TF_WEIGHTS[tf],
                "threshold": _TF_ROC_THRESHOLD[tf],
            }
            for tf in MOMENTUM_TIMEFRAMES
        },
        "signal": signal,
    }

    return MomentumResult(signal=signal, rationale=rationale)
