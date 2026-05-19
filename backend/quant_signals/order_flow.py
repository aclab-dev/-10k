"""Señal order flow imbalance multi-timeframe (épica F5, tarjeta 51).

Sin datos de L2, aproxima el imbalance usando la posición del cierre dentro
del rango high-low de cada vela (una vela por timeframe, la más reciente del snapshot):

    ofi = (2*close - high - low) / (high - low)

+1.0 → cierre en el máximo (presión compradora total)
-1.0 → cierre en el mínimo (presión vendedora total)
 0.0 → cierre en el punto medio (equilibrio)

Cuando high == low (vela doji), el imbalance es 0.0 por definición.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from backend.market_data.schemas import CandleData, MarketSnapshot

_TF_WEIGHTS: Final[dict[str, float]] = {
    "5m": 0.10,
    "15m": 0.20,
    "1h": 0.30,
    "4h": 0.40,
}

ORDER_FLOW_TIMEFRAMES: Final[tuple[str, ...]] = ("5m", "15m", "1h", "4h")


@dataclass(frozen=True)
class OrderFlowResult:
    """Output del cálculo de señal order flow imbalance."""

    signal: float
    rationale: dict[str, Any]


def _candle_ofi(candle: CandleData) -> float:
    """Imbalance de una vela individual en [-1.0, 1.0].

    Usa (2*close - high - low) / (high - low) como proxy de presión compradora
    vs. vendedora cuando no hay datos de order book L2.
    """
    price_range = candle.high - candle.low
    if price_range == Decimal("0"):
        return 0.0
    return float((2 * candle.close - candle.high - candle.low) / price_range)


def calculate_order_flow_imbalance(snapshot: MarketSnapshot) -> OrderFlowResult:
    """Calcula la señal de order flow imbalance multi-timeframe.

    Algoritmo:
      1. Por cada TF: ofi = (2*close - high - low) / (high - low)  → [-1, 1]
      2. Agregar: signal = Σ weight_tf * ofi_tf                     → [-1, 1]

    El resultado es reproducible y determinístico.
    """
    candles_by_tf: dict[str, CandleData] = {
        "5m": snapshot.candles.tf_5m,
        "15m": snapshot.candles.tf_15m,
        "1h": snapshot.candles.tf_1h,
        "4h": snapshot.candles.tf_4h,
    }

    per_tf_ofi: dict[str, float] = {
        tf: _candle_ofi(candles_by_tf[tf]) for tf in ORDER_FLOW_TIMEFRAMES
    }

    raw_signal = sum(_TF_WEIGHTS[tf] * per_tf_ofi[tf] for tf in ORDER_FLOW_TIMEFRAMES)
    signal = max(-1.0, min(1.0, raw_signal))

    rationale: dict[str, Any] = {
        "method": "weighted_close_position_in_range",
        "timeframes": {
            tf: {
                "ofi": per_tf_ofi[tf],
                "weight": _TF_WEIGHTS[tf],
            }
            for tf in ORDER_FLOW_TIMEFRAMES
        },
        "signal": signal,
    }

    return OrderFlowResult(signal=signal, rationale=rationale)
