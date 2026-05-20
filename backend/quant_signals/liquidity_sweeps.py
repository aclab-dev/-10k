"""Señal liquidity sweeps — detección de barridos de liquidez (stop hunts) por TF.

Método: análisis de imbalance de mechas intra-vela multi-timeframe.

Un liquidity sweep (stop hunt) deja una mecha larga en la dirección del barrido:
  - Mecha inferior dominante → precio perforó niveles inferiores y revirtió → señal alcista
  - Mecha superior dominante → precio perforó niveles superiores y revirtió → señal bajista

Score intra-vela por TF:
  lower_wick    = min(open, close) − low
  upper_wick    = high − max(open, close)
  candle_range  = high − low
  wick_imbalance = (lower_wick − upper_wick) / candle_range   → en [−1.0, 1.0]
  wick_fraction  = (lower_wick + upper_wick) / candle_range   → en [0.0, 1.0]
  raw_score      = wick_imbalance × wick_fraction              → en [−1.0, 1.0]
  normalized     = tanh(raw_score / threshold_tf)             → en [−1.0, 1.0]

Señal agregada = Σ weight_tf × normalized_tf → en [−1.0, 1.0]
  Positivo = señal alcista (sweep hacia abajo, precio revirtió arriba)
  Negativo = señal bajista (sweep hacia arriba, precio revirtió abajo)

Limitación conocida: sin historia de velas, no se puede comparar contra niveles previos
reales (swing highs/lows de sesiones anteriores). Se usa la asimetría de mechas como
proxy del patrón de rechazo post-sweep. Una ventana rolling en CandleData permitiría
detección sobre niveles históricos concretos.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

from backend.market_data.schemas import CandleData, MarketSnapshot

# Pesos por timeframe (suma = 1.0). Mayor peso a TFs más largos.
_TF_WEIGHTS: Final[dict[str, float]] = {
    "5m": 0.10,
    "15m": 0.20,
    "1h": 0.30,
    "4h": 0.40,
}

# Threshold de raw_score: tanh(raw_score / threshold) ≈ 0.76 cuando raw_score == threshold.
# Calibrado para que una vela con mecha dominante (wick_imbalance ≈ 0.5, wick_fraction ≈ 0.6)
# produzca raw_score ≈ 0.30 → señal ≈ 0.76 con threshold = 0.30.
_TF_SWEEP_THRESHOLD: Final[dict[str, float]] = {
    "5m": 0.25,
    "15m": 0.25,
    "1h": 0.28,
    "4h": 0.30,
}

# Rango mínimo de la vela: evita división por cero en candles doji flat.
_MIN_RANGE: Final[float] = 1e-8

LIQUIDITY_SWEEP_TIMEFRAMES: Final[tuple[str, ...]] = ("5m", "15m", "1h", "4h")


@dataclass(frozen=True)
class LiquiditySweepResult:
    """Output del cálculo de señal liquidity sweeps."""

    signal: float  # [-1.0, 1.0]. Positivo = bullish sweep, negativo = bearish sweep.
    rationale: dict[str, Any]  # desglose auditable por timeframe


def _wick_metrics(candle: CandleData) -> tuple[float, float, float]:
    """Retorna (candle_range, lower_wick, upper_wick) como floats."""
    high = float(candle.high)
    low = float(candle.low)
    open_ = float(candle.open)
    close = float(candle.close)
    body_top = max(open_, close)
    body_bot = min(open_, close)
    return high - low, body_bot - low, high - body_top


def calculate_liquidity_sweeps(snapshot: MarketSnapshot) -> LiquiditySweepResult:
    """Calcula la señal de liquidity sweeps multi-timeframe a partir de un MarketSnapshot.

    Algoritmo:
      1. Por cada TF: calcular raw_score = wick_imbalance × wick_fraction
      2. Normalizar: tanh(raw_score / threshold_tf)
      3. Agregar: signal = Σ weight_tf × normalized_tf

    El resultado es reproducible y determinístico: dada la misma entrada,
    siempre produce la misma salida.
    """
    candles_by_tf: dict[str, CandleData] = {
        "5m": snapshot.candles.tf_5m,
        "15m": snapshot.candles.tf_15m,
        "1h": snapshot.candles.tf_1h,
        "4h": snapshot.candles.tf_4h,
    }

    per_tf: dict[str, dict[str, float]] = {}

    for tf in LIQUIDITY_SWEEP_TIMEFRAMES:
        candle = candles_by_tf[tf]
        candle_range, lower_wick, upper_wick = _wick_metrics(candle)
        threshold = _TF_SWEEP_THRESHOLD[tf]

        if candle_range >= _MIN_RANGE:
            wick_imbalance = (lower_wick - upper_wick) / candle_range
            wick_fraction = (lower_wick + upper_wick) / candle_range
            raw_score = wick_imbalance * wick_fraction
            normalized = math.tanh(raw_score / threshold)
        else:
            wick_imbalance = 0.0
            wick_fraction = 0.0
            raw_score = 0.0
            normalized = 0.0

        per_tf[tf] = {
            "candle_range": candle_range,
            "lower_wick": lower_wick,
            "upper_wick": upper_wick,
            "wick_imbalance": wick_imbalance,
            "wick_fraction": wick_fraction,
            "raw_score": raw_score,
            "normalized": normalized,
            "weight": _TF_WEIGHTS[tf],
            "threshold": threshold,
        }

    raw_signal = sum(
        _TF_WEIGHTS[tf] * per_tf[tf]["normalized"] for tf in LIQUIDITY_SWEEP_TIMEFRAMES
    )
    signal = max(-1.0, min(1.0, raw_signal))

    rationale: dict[str, Any] = {
        "method": "multi_tf_wick_imbalance",
        "timeframes": per_tf,
        "signal": signal,
    }

    return LiquiditySweepResult(signal=signal, rationale=rationale)
