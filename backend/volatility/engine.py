"""Volatility Engine — cálculo de ATR, realized vol y clasificación de régimen (épica F6)."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Final

from backend.market_data.schemas import CandleData, MarketSnapshot
from backend.volatility.schemas import VolatilityAssessmentPackage, VolatilityRegime

# ---------------------------------------------------------------------------
# Calibración
# ---------------------------------------------------------------------------

# Umbral de ROC promedio que produce volatility_score ≈ 0.76 (señal fuerte).
# 1% de movimiento intra-vela es representativo de alta actividad en crypto futures.
_REALIZED_VOL_THRESHOLD: Final[float] = 0.01

# Score mínimo para clasificar como EXPANSION (mercado activo/volátil).
_EXPANSION_THRESHOLD: Final[float] = 0.5

# Escalonado de leverage_cap por banda de volatility_score.
# Cada tupla: (score_máximo_inclusive, leverage_cap).
# El Risk Engine aplica adicionalmente los caps de entorno (PAPER ≤10x, LIVE ≤5x).
_LEVERAGE_BANDS: Final[tuple[tuple[float, int], ...]] = (
    (0.25, 10),
    (0.50, 7),
    (0.75, 5),
    (1.00, 3),
)

# Peso relativo del ATR% en el cálculo de liquidation_risk_score.
# Capado en 1%: ATR > 1% del precio ya implica riesgo de liquidación apreciable.
_ATR_PERCENT_CAP: Final[float] = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atr_from_candle(candle: CandleData) -> Decimal:
    """True Range aproximado con vela única: High − Low (sin prev_close)."""
    return candle.high - candle.low


def _abs_roc(candle: CandleData) -> float:
    """ROC intra-vela en valor absoluto: |close − open| / open.

    Mide solo el movimiento del cuerpo de la vela. No captura volatilidad de mechas.
    """
    if candle.open == Decimal("0"):
        return 0.0
    return float(abs(candle.close - candle.open) / candle.open)


def _range_vol(candle: CandleData) -> float:
    """Volatilidad de rango intra-vela: (High − Low) / open.

    Captura el movimiento total de la vela (cuerpo + mechas). Siempre ≥ _abs_roc.
    Uso: proxy de realized_vol que detecta tanto cuerpos grandes como dojis de
    mecha larga (wide range, flat body), caso ignorado por _abs_roc.
    """
    if candle.open == Decimal("0"):
        return 0.0
    return float((candle.high - candle.low) / candle.open)


def _leverage_cap_for_score(score: float) -> int:
    """Devuelve el leverage_cap correspondiente a un volatility_score dado."""
    for max_score, cap in _LEVERAGE_BANDS:
        if score <= max_score:
            return cap
    return _LEVERAGE_BANDS[-1][1]  # fallback al más conservador


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------


def compute_volatility_assessment(snapshot: MarketSnapshot) -> VolatilityAssessmentPackage:
    """Calcula el assessment de volatilidad para un MarketSnapshot.

    Algoritmo:
      1. ATR por TF = High − Low (True Range aproximado, vela única).
      2. realized_vol = media((H−L)/open) en los 4 TFs — captura rango completo,
         incluyendo dojis de mecha larga (wide range, flat body).
      3. volatility_score = tanh(realized_vol / threshold) → [0.0, 1.0].
      4. Régimen = EXPANSION si score ≥ threshold, CONTRACTION si no.
      5. leverage_cap = escalonado según score.
      6. liquidation_risk_score = promedio ponderado de score y ATR%.

    Determinístico: misma entrada → misma salida.
    """
    candles: dict[str, CandleData] = {
        "5m": snapshot.candles.tf_5m,
        "15m": snapshot.candles.tf_15m,
        "1h": snapshot.candles.tf_1h,
        "4h": snapshot.candles.tf_4h,
    }

    # 1. ATR por timeframe
    atrs: dict[str, Decimal] = {tf: _atr_from_candle(c) for tf, c in candles.items()}

    # 2. ATR 1h como % del last_price
    atr_percent = float(atrs["1h"] / snapshot.last_price) * 100.0

    # 3. Realized vol: media de (H−L)/open en los 4 TFs.
    # Se usa el rango completo (cuerpo + mechas) para capturar tanto cuerpos grandes
    # como dojis de mecha larga (ej. open=close=50000, high=60000, low=40000).
    range_vols: dict[str, float] = {tf: _range_vol(c) for tf, c in candles.items()}
    realized_vol = sum(range_vols.values()) / len(range_vols)

    # 4. Volatility score [0.0, 1.0] — tanh con clip defensivo
    raw_score = math.tanh(realized_vol / _REALIZED_VOL_THRESHOLD)
    volatility_score = max(0.0, min(1.0, raw_score))

    # 5. Clasificación de régimen
    regime = (
        VolatilityRegime.EXPANSION
        if volatility_score >= _EXPANSION_THRESHOLD
        else VolatilityRegime.CONTRACTION
    )

    # 6. Leverage cap
    leverage_cap = _leverage_cap_for_score(volatility_score)

    # 7. Liquidation risk: promedio de volatility_score y ATR% normalizado
    atr_percent_norm = min(1.0, atr_percent / _ATR_PERCENT_CAP)
    liquidation_risk_score = max(0.0, min(1.0, (volatility_score + atr_percent_norm) / 2.0))

    details: dict[str, object] = {
        "method": "single_candle_range_proxy",
        "timeframes": {
            tf: {
                "atr": str(atrs[tf]),
                "range_vol": range_vols[tf],
            }
            for tf in ("5m", "15m", "1h", "4h")
        },
        "realized_vol_threshold": _REALIZED_VOL_THRESHOLD,
        "expansion_threshold": _EXPANSION_THRESHOLD,
        "atr_percent_1h": atr_percent,
    }

    return VolatilityAssessmentPackage(
        snapshot_id=snapshot.snapshot_id,
        timestamp_utc=snapshot.timestamp_utc,
        symbol=snapshot.symbol,
        atr_5m=atrs["5m"],
        atr_15m=atrs["15m"],
        atr_1h=atrs["1h"],
        atr_4h=atrs["4h"],
        atr_percent=atr_percent,
        realized_vol=realized_vol,
        volatility_regime=regime,
        volatility_score=volatility_score,
        leverage_cap=leverage_cap,
        liquidation_risk_score=liquidation_risk_score,
        details=details,
    )
