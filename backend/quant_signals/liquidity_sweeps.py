"""Señal liquidity sweeps — detección de barridos de liquidez (stop hunts) por TF.

Cuando hay historia de velas (≥ _MIN_HISTORY_LEN por TF) se usa detección sobre
niveles reales: swing highs/lows de las velas anteriores. Si no hay historia
suficiente se aplica el método proxy de asimetría de mechas intra-vela.

Método proxy (sin historia):
  lower_wick    = min(open, close) − low
  upper_wick    = high − max(open, close)
  candle_range  = high − low
  wick_imbalance = (lower_wick − upper_wick) / candle_range   → en [−1.0, 1.0]
  wick_fraction  = (lower_wick + upper_wick) / candle_range   → en [0.0, 1.0]
  raw_score      = wick_imbalance × wick_fraction              → en [−1.0, 1.0]
  normalized     = tanh(raw_score / threshold_tf)             → en [−1.0, 1.0]

Método histórico (con historia):
  Se identifican swing highs/lows en la ventana de velas anteriores.
  Bullish sweep: low de la vela actual < swing_low más cercano Y close > ese nivel.
  Bearish sweep: high de la vela actual > swing_high más cercano Y close < ese nivel.
  El score refleja profundidad de perforación y magnitud de recuperación.
  Se blendea con el proxy: _HISTORICAL_WEIGHT * histórico + (1−_HISTORICAL_WEIGHT) * proxy.

Señal agregada = Σ weight_tf × score_tf → en [−1.0, 1.0]
  Positivo = señal alcista (sweep hacia abajo, precio revirtió arriba)
  Negativo = señal bajista (sweep hacia arriba, precio revirtió abajo)
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

# Threshold de raw_score para el método proxy.
_TF_SWEEP_THRESHOLD: Final[dict[str, float]] = {
    "5m": 0.25,
    "15m": 0.25,
    "1h": 0.28,
    "4h": 0.30,
}

_MIN_RANGE: Final[float] = 1e-8

# Velas a cada lado para clasificar un pivote como swing high/low.
_SWING_LOOKBACK: Final[int] = 3

# Historia mínima (en velas) para activar detección histórica en un TF.
_MIN_HISTORY_LEN: Final[int] = 5

# Peso del score histórico vs wick-proxy cuando hay historia suficiente.
_HISTORICAL_WEIGHT: Final[float] = 0.6

LIQUIDITY_SWEEP_TIMEFRAMES: Final[tuple[str, ...]] = ("5m", "15m", "1h", "4h")


@dataclass(frozen=True)
class LiquiditySweepResult:
    """Output del cálculo de señal liquidity sweeps."""

    signal: float  # [-1.0, 1.0]. Positivo = bullish sweep, negativo = bearish sweep.
    rationale: dict[str, Any]  # desglose auditable por timeframe


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _wick_metrics(candle: CandleData) -> tuple[float, float, float]:
    """Retorna (candle_range, lower_wick, upper_wick) como floats."""
    high = float(candle.high)
    low = float(candle.low)
    open_ = float(candle.open)
    close = float(candle.close)
    body_top = max(open_, close)
    body_bot = min(open_, close)
    return high - low, body_bot - low, high - body_top


def _proxy_score(candle: CandleData, threshold: float) -> tuple[float, dict[str, float]]:
    """Calcula el score por asimetría de mechas (método proxy).

    Retorna (score_normalizado, métricas_para_rationale).
    """
    candle_range, lower_wick, upper_wick = _wick_metrics(candle)

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

    return normalized, {
        "candle_range": candle_range,
        "lower_wick": lower_wick,
        "upper_wick": upper_wick,
        "wick_imbalance": wick_imbalance,
        "wick_fraction": wick_fraction,
        "raw_score": raw_score,
        "normalized": normalized,
    }


def _find_swing_lows(history: tuple[CandleData, ...], lookback: int) -> list[float]:
    """Retorna los swing lows de la ventana histórica.

    Un swing low en el índice i satisface: history[i].low < history[j].low
    para todo j en [i-lookback, i+lookback] con j != i.
    """
    lows: list[float] = []
    n = len(history)
    for i in range(lookback, n - lookback):
        candidate = float(history[i].low)
        neighbors = range(i - lookback, i + lookback + 1)
        if all(candidate < float(history[j].low) for j in neighbors if j != i):
            lows.append(candidate)
    return lows


def _find_swing_highs(history: tuple[CandleData, ...], lookback: int) -> list[float]:
    """Retorna los swing highs de la ventana histórica."""
    highs: list[float] = []
    n = len(history)
    for i in range(lookback, n - lookback):
        candidate = float(history[i].high)
        neighbors = range(i - lookback, i + lookback + 1)
        if all(candidate > float(history[j].high) for j in neighbors if j != i):
            highs.append(candidate)
    return highs


def _historical_score(
    candle: CandleData,
    history: tuple[CandleData, ...],
    lookback: int,
) -> tuple[float, dict[str, Any]]:
    """Score basado en perforación y recuperación de swing levels históricos.

    Bullish: low perfora un swing low y close lo recupera.
    Bearish: high perfora un swing high y close lo abandona (cierra bajo el nivel).
    Retorna (score en [-1,1], métricas_para_rationale).
    """
    swing_lows = _find_swing_lows(history, lookback)
    swing_highs = _find_swing_highs(history, lookback)

    candle_low = float(candle.low)
    candle_high = float(candle.high)
    candle_close = float(candle.close)
    candle_range = candle_high - candle_low

    best_bull = 0.0
    swept_low: float | None = None
    for level in swing_lows:
        if candle_low < level and candle_close > level:
            # Profundidad de perforación relativa + magnitud de recuperación
            denom = candle_range if candle_range >= _MIN_RANGE else 1.0
            penetration = (level - candle_low) / denom
            recovery = (candle_close - level) / denom
            score = math.tanh(penetration + recovery)
            if score > best_bull:
                best_bull = score
                swept_low = level

    best_bear = 0.0
    swept_high: float | None = None
    for level in swing_highs:
        if candle_high > level and candle_close < level:
            denom = candle_range if candle_range >= _MIN_RANGE else 1.0
            penetration = (candle_high - level) / denom
            recovery = (level - candle_close) / denom
            score = math.tanh(penetration + recovery)
            if score > best_bear:
                best_bear = score
                swept_high = level

    # Señal neta: bullish positivo, bearish negativo
    if best_bull >= best_bear:
        net_score = best_bull
    else:
        net_score = -best_bear

    return net_score, {
        "swing_lows": swing_lows,
        "swing_highs": swing_highs,
        "swept_low": swept_low,
        "swept_high": swept_high,
        "historical_score": net_score,
    }


# ---------------------------------------------------------------------------
# Función pública
# ---------------------------------------------------------------------------


def calculate_liquidity_sweeps(snapshot: MarketSnapshot) -> LiquiditySweepResult:
    """Calcula la señal de liquidity sweeps multi-timeframe.

    Por cada TF:
    - Si hay historia suficiente (≥ _MIN_HISTORY_LEN velas): blendea score
      histórico (swing levels) con proxy (wick imbalance).
    - Si no: usa solo el proxy.

    El resultado es reproducible y determinístico.
    """
    candles_by_tf: dict[str, CandleData] = {
        "5m": snapshot.candles.tf_5m,
        "15m": snapshot.candles.tf_15m,
        "1h": snapshot.candles.tf_1h,
        "4h": snapshot.candles.tf_4h,
    }
    history_by_tf: dict[str, tuple[CandleData, ...]] = {
        "5m": snapshot.candles.history_5m,
        "15m": snapshot.candles.history_15m,
        "1h": snapshot.candles.history_1h,
        "4h": snapshot.candles.history_4h,
    }

    per_tf: dict[str, dict[str, Any]] = {}

    for tf in LIQUIDITY_SWEEP_TIMEFRAMES:
        candle = candles_by_tf[tf]
        history = history_by_tf[tf]
        threshold = _TF_SWEEP_THRESHOLD[tf]

        proxy, proxy_metrics = _proxy_score(candle, threshold)

        hist_score: float
        hist_metrics: dict[str, Any]

        has_history = len(history) >= _MIN_HISTORY_LEN
        if has_history:
            hist_score, hist_metrics = _historical_score(candle, history, _SWING_LOOKBACK)
            blended = _HISTORICAL_WEIGHT * hist_score + (1.0 - _HISTORICAL_WEIGHT) * proxy
            detection_method = "historical_levels"
        else:
            hist_score = 0.0
            hist_metrics = {
                "swing_lows": [],
                "swing_highs": [],
                "swept_low": None,
                "swept_high": None,
                "historical_score": 0.0,
            }
            blended = proxy
            detection_method = "wick_proxy"

        per_tf[tf] = {
            **proxy_metrics,
            "weight": _TF_WEIGHTS[tf],
            "threshold": threshold,
            "detection_method": detection_method,
            "historical": hist_metrics,
            "blended_score": blended,
        }

    raw_signal = sum(
        _TF_WEIGHTS[tf] * per_tf[tf]["blended_score"] for tf in LIQUIDITY_SWEEP_TIMEFRAMES
    )
    signal = max(-1.0, min(1.0, raw_signal))

    rationale: dict[str, Any] = {
        "method": "multi_tf_wick_imbalance",
        "timeframes": per_tf,
        "signal": signal,
    }

    return LiquiditySweepResult(signal=signal, rationale=rationale)
