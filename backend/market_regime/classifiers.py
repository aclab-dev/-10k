"""Clasificadores de régimen de mercado — reglas explícitas basadas en MarketSnapshot.

Todas las funciones son puras: mismo input → mismo output. Sin estado interno.
Consume un MarketSnapshot (4.10.1) y produce las partes del MarketRegimeAssessment.
"""

from __future__ import annotations

from backend.market_data.schemas import CandleData, MarketSnapshot
from backend.market_regime.schemas import (
    FundingState,
    LiquidityState,
    OpenInterestState,
    PrimaryRegime,
    TrendAlignment,
    VolatilityState,
)

# ---------------------------------------------------------------------------
# Umbrales (centralizados para facilitar ajuste y auditoría)
# ---------------------------------------------------------------------------

# Volatilidad: rango de la vela 1h como % del precio de cierre
_HIGH_VOL_RANGE_PCT: float = 4.0
_LOW_VOL_RANGE_PCT: float = 0.3

# Liquidez: spread como % del precio
_HIGH_SPREAD_PCT: float = 1.5  # spread alto → liquidez baja
_LOW_SPREAD_PCT: float = 0.05  # spread bajo → liquidez alta

# Cuerpo de vela: proporción body/range [0, 1]
_BODY_RATIO_STRONG: float = 0.60  # cuerpo dominante
_BODY_RATIO_WEAK: float = 0.35  # cuerpo débil

# Posición del cierre dentro del rango de la vela [0, 1]
_CLOSE_POS_BULLISH: float = 0.70  # cierre en top 30% → alcista
_CLOSE_POS_BEARISH: float = 0.30  # cierre en bottom 30% → bajista

# Breakout
_BREAKOUT_BODY_RATIO: float = 0.75  # body_ratio mínimo para breakout
_BREAKOUT_RANGE_PCT: float = 1.5  # range 1h mínimo (%) para breakout

# Trending
_TREND_BODY_1H: float = 0.50
_TREND_BODY_4H: float = 0.40

# Funding rate (como decimal, no porcentaje)
_FUNDING_EXTREME: float = 0.002  # |rate| > 0.2% → EXTREME
_FUNDING_POSITIVE: float = 0.0001  # rate > 0.01% → POSITIVE
_FUNDING_NEGATIVE: float = -0.0001  # rate < -0.01% → NEGATIVE


# ---------------------------------------------------------------------------
# Helpers internos de candle
# ---------------------------------------------------------------------------


def _candle_metrics(candle: CandleData) -> tuple[float, float]:
    """Devuelve (body_ratio, close_position) para una vela.

    body_ratio en [0, 1]: proporción del cuerpo respecto al rango total.
    close_position en [0, 1]: posición del cierre en el rango (0=low, 1=high).
    """
    candle_range = float(candle.high - candle.low)
    if candle_range == 0.0:
        return 0.0, 0.5
    body = abs(float(candle.close - candle.open))
    body_ratio = body / candle_range
    close_position = float(candle.close - candle.low) / candle_range
    return body_ratio, close_position


def _range_pct(candle: CandleData) -> float:
    """Rango de la vela como porcentaje del precio de cierre."""
    close = float(candle.close)
    if close == 0.0:
        return 0.0
    return float(candle.high - candle.low) / close * 100.0


def _is_bullish_close(close_position: float) -> bool:
    return close_position >= _CLOSE_POS_BULLISH


def _is_bearish_close(close_position: float) -> bool:
    return close_position <= _CLOSE_POS_BEARISH


# ---------------------------------------------------------------------------
# Clasificadores de estados auxiliares
# ---------------------------------------------------------------------------


def assess_volatility_state(snapshot: MarketSnapshot) -> VolatilityState:
    """Clasifica volatilidad en HIGH / NORMAL / LOW según rango de vela 1h."""
    range_1h_pct = _range_pct(snapshot.candles.tf_1h)
    if range_1h_pct >= _HIGH_VOL_RANGE_PCT:
        return VolatilityState.HIGH
    if range_1h_pct <= _LOW_VOL_RANGE_PCT:
        return VolatilityState.LOW
    return VolatilityState.NORMAL


def assess_liquidity_state(snapshot: MarketSnapshot) -> LiquidityState:
    """Clasifica liquidez en HIGH / NORMAL / LOW según spread porcentual."""
    spread_pct = float(snapshot.spread_percent)
    if spread_pct >= _HIGH_SPREAD_PCT:
        return LiquidityState.LOW
    if spread_pct <= _LOW_SPREAD_PCT:
        return LiquidityState.HIGH
    return LiquidityState.NORMAL


def assess_funding_state(snapshot: MarketSnapshot) -> FundingState:
    """Clasifica el estado del funding rate."""
    rate = snapshot.funding_rate
    if rate is None:
        return FundingState.NEUTRAL

    if abs(rate) >= _FUNDING_EXTREME:
        return FundingState.EXTREME
    if rate >= _FUNDING_POSITIVE:
        return FundingState.POSITIVE
    if rate <= _FUNDING_NEGATIVE:
        return FundingState.NEGATIVE
    return FundingState.NEUTRAL


def assess_open_interest_state(snapshot: MarketSnapshot) -> OpenInterestState:
    """Clasifica el estado del open interest.

    Con un único snapshot no se puede determinar RISING/FALLING sin referencia
    histórica, por lo que se reporta STABLE cuando el dato está presente y
    UNKNOWN cuando no hay dato.
    """
    if snapshot.open_interest is None:
        return OpenInterestState.UNKNOWN
    return OpenInterestState.STABLE


def assess_trend_alignment(snapshot: MarketSnapshot) -> TrendAlignment:
    """Evalúa la alineación de tendencia a través de los cuatro timeframes.

    Cuenta cuántos timeframes muestran cierre alcista o bajista. Si tres o
    más coinciden en la misma dirección, se reporta esa dirección. Con señales
    contradictorias se reporta MIXED; sin señal clara, NEUTRAL.
    """
    timeframes = [
        snapshot.candles.tf_5m,
        snapshot.candles.tf_15m,
        snapshot.candles.tf_1h,
        snapshot.candles.tf_4h,
    ]

    bullish_count = 0
    bearish_count = 0
    for candle in timeframes:
        _, close_pos = _candle_metrics(candle)
        if _is_bullish_close(close_pos):
            bullish_count += 1
        elif _is_bearish_close(close_pos):
            bearish_count += 1

    if bullish_count >= 3:
        return TrendAlignment.BULLISH
    if bearish_count >= 3:
        return TrendAlignment.BEARISH
    if bullish_count > 0 and bearish_count > 0:
        return TrendAlignment.MIXED
    return TrendAlignment.NEUTRAL


# ---------------------------------------------------------------------------
# Clasificador de régimen secundario
# ---------------------------------------------------------------------------


def classify_secondary_regime(
    primary: PrimaryRegime,
    volatility_state: VolatilityState,
    trend_alignment: TrendAlignment,
) -> PrimaryRegime | None:
    """Determina el régimen secundario como contexto adicional al primario."""
    if primary == PrimaryRegime.BREAKOUT:
        # Un breakout implica movimiento tendencial
        return PrimaryRegime.TRENDING

    if primary in (PrimaryRegime.TRENDING, PrimaryRegime.RANGING):
        if volatility_state == VolatilityState.HIGH:
            return PrimaryRegime.HIGH_VOLATILITY
        if volatility_state == VolatilityState.LOW:
            return PrimaryRegime.LOW_VOLATILITY

    if primary == PrimaryRegime.UNCLEAR:
        if trend_alignment in (TrendAlignment.BULLISH, TrendAlignment.BEARISH):
            return PrimaryRegime.TRENDING

    return None


# ---------------------------------------------------------------------------
# Clasificador de régimen primario
# ---------------------------------------------------------------------------


def classify_primary_regime(
    snapshot: MarketSnapshot,
    volatility_state: VolatilityState,
    trend_alignment: TrendAlignment,
) -> tuple[PrimaryRegime, float, list[str]]:
    """Determina el régimen primario y su confianza.

    Devuelve (regime, confidence, notes). Las reglas se evalúan en orden de
    prioridad; la primera que se cumple determina el régimen.

    Prioridad:
      1. HIGH_VOLATILITY  — rangos extremos que dominan cualquier otro patrón
      2. LOW_VOLATILITY   — mercado dormido, sin impulso
      3. BREAKOUT         — vela con cuerpo muy dominante y cierre en extremo
      4. TRENDING         — dirección sostenida en 1h y 4h con alineación multi-TF
      5. RANGING          — cuerpo débil y cierre en zona media
      6. UNCLEAR          — ninguna señal dominante (fallback)
    """
    notes: list[str] = []

    body_ratio_1h, close_pos_1h = _candle_metrics(snapshot.candles.tf_1h)
    body_ratio_4h, _ = _candle_metrics(snapshot.candles.tf_4h)
    range_1h_pct = _range_pct(snapshot.candles.tf_1h)

    # --- 1. HIGH_VOLATILITY ---
    if volatility_state == VolatilityState.HIGH:
        notes.append(f"range_1h_pct={range_1h_pct:.2f}% >= umbral {_HIGH_VOL_RANGE_PCT}%")
        excess = range_1h_pct - _HIGH_VOL_RANGE_PCT
        confidence = round(min(1.0, 0.6 + excess / _HIGH_VOL_RANGE_PCT), 3)
        return PrimaryRegime.HIGH_VOLATILITY, confidence, notes

    # --- 2. LOW_VOLATILITY ---
    if volatility_state == VolatilityState.LOW:
        notes.append(f"range_1h_pct={range_1h_pct:.2f}% <= umbral {_LOW_VOL_RANGE_PCT}%")
        margin = _LOW_VOL_RANGE_PCT - range_1h_pct
        confidence = round(min(1.0, 0.6 + margin / _LOW_VOL_RANGE_PCT), 3)
        return PrimaryRegime.LOW_VOLATILITY, confidence, notes

    # --- 3. BREAKOUT ---
    # Cuerpo muy dominante + cierre en extremo del rango + rango significativo
    is_extreme_close = _is_bullish_close(close_pos_1h) or _is_bearish_close(close_pos_1h)
    if (
        body_ratio_1h >= _BREAKOUT_BODY_RATIO
        and is_extreme_close
        and range_1h_pct >= _BREAKOUT_RANGE_PCT
    ):
        notes.append(
            f"body_ratio_1h={body_ratio_1h:.2f} >= {_BREAKOUT_BODY_RATIO}, "
            f"close_pos_1h={close_pos_1h:.2f}, range_1h_pct={range_1h_pct:.2f}%"
        )
        confidence = round(min(1.0, body_ratio_1h * 0.5 + 0.5), 3)
        return PrimaryRegime.BREAKOUT, confidence, notes

    # --- 4. TRENDING ---
    # Cuerpos fuertes en 1h y 4h con alineación multi-timeframe
    trend_is_aligned = trend_alignment in (TrendAlignment.BULLISH, TrendAlignment.BEARISH)
    if body_ratio_1h >= _TREND_BODY_1H and body_ratio_4h >= _TREND_BODY_4H and trend_is_aligned:
        notes.append(
            f"body_ratio_1h={body_ratio_1h:.2f}, body_ratio_4h={body_ratio_4h:.2f}, "
            f"alignment={trend_alignment}"
        )
        confidence = round(min(1.0, (body_ratio_1h + body_ratio_4h) / 2 * 0.8 + 0.2), 3)
        return PrimaryRegime.TRENDING, confidence, notes

    # --- 5. RANGING ---
    # Cuerpo débil + cierre en zona media
    is_mid_close = _CLOSE_POS_BEARISH < close_pos_1h < _CLOSE_POS_BULLISH
    if body_ratio_1h <= _BODY_RATIO_WEAK and is_mid_close:
        notes.append(
            f"body_ratio_1h={body_ratio_1h:.2f} <= {_BODY_RATIO_WEAK}, "
            f"close_pos_1h={close_pos_1h:.2f} en zona media"
        )
        margin = _BODY_RATIO_WEAK - body_ratio_1h
        confidence = round(min(1.0, 0.5 + margin / _BODY_RATIO_WEAK * 0.5), 3)
        return PrimaryRegime.RANGING, confidence, notes

    # --- 6. UNCLEAR (fallback) ---
    notes.append(
        f"Sin señal dominante: body_ratio_1h={body_ratio_1h:.2f}, "
        f"close_pos_1h={close_pos_1h:.2f}, alignment={trend_alignment}"
    )
    return PrimaryRegime.UNCLEAR, 0.3, notes
