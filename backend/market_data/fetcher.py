"""Interfaz DataFetcher y MockDataFetcher para PAPER.

La interfaz permite intercambiar implementaciones mock/real sin cambiar
el código del Market Data Engine. El mock genera snapshots realistas
con ruido estadístico para simular movimientos de mercado en PAPER.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from backend.core.config import Environment
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)

# ---------------------------------------------------------------------------
# Parámetros de simulación por símbolo
# ---------------------------------------------------------------------------

_BASE_PRICES: dict[str, Decimal] = {
    "BTCUSDT": Decimal("65000"),
    "ETHUSDT": Decimal("3200"),
    "BNBUSDT": Decimal("580"),
    "SOLUSDT": Decimal("160"),
    "XRPUSDT": Decimal("0.55"),
}

_BASE_VOLUMES: dict[str, Decimal] = {
    "BTCUSDT": Decimal("2500"),
    "ETHUSDT": Decimal("18000"),
    "BNBUSDT": Decimal("12000"),
    "SOLUSDT": Decimal("85000"),
    "XRPUSDT": Decimal("9000000"),
}

_BASE_OPEN_INTEREST: dict[str, Decimal] = {
    "BTCUSDT": Decimal("45000"),
    "ETHUSDT": Decimal("820000"),
    "BNBUSDT": Decimal("180000"),
    "SOLUSDT": Decimal("6000000"),
    "XRPUSDT": Decimal("350000000"),
}

_TICK_SIZES: dict[str, Decimal] = {
    "BTCUSDT": Decimal("0.1"),
    "ETHUSDT": Decimal("0.01"),
    "BNBUSDT": Decimal("0.01"),
    "SOLUSDT": Decimal("0.001"),
    "XRPUSDT": Decimal("0.0001"),
}


# ---------------------------------------------------------------------------
# Interfaz
# ---------------------------------------------------------------------------


class DataFetcher(ABC):
    """Contrato que todo fetcher de datos de mercado debe cumplir.

    El Market Data Engine trabaja contra esta interfaz; el entorno determina
    qué implementación se inyecta (mock para PAPER, real para TESTNET/LIVE).
    """

    @abstractmethod
    async def fetch_snapshot(
        self,
        symbol: str,
        account_balance_usdt: Decimal,
        open_positions_count: int = 0,
        active_orders_count: int = 0,
    ) -> MarketSnapshot:
        """Obtiene un snapshot completo de mercado para el símbolo dado.

        Args:
            symbol: Par de trading (debe estar en el whitelist permitido).
            account_balance_usdt: Balance disponible en USDT de la cuenta.
            open_positions_count: Posiciones abiertas actuales.
            active_orders_count: Órdenes activas actuales.
        """

    @abstractmethod
    def is_healthy(self) -> bool:
        """True si la fuente de datos está disponible y respondiendo."""


# ---------------------------------------------------------------------------
# Mock fetcher para PAPER
# ---------------------------------------------------------------------------


class MockDataFetcher(DataFetcher):
    """Genera snapshots realistas con ruido estadístico para PAPER.

    No llama a ningún exchange. Simula movimientos de precio, spread,
    funding rate y open interest dentro de rangos estadísticamente
    representativos de los mercados reales.
    """

    def __init__(
        self,
        *,
        seed: int | None = None,
        spread_bps: float = 2.0,
        volatility_pct: float = 0.3,
        simulated_latency_ms: int = 35,
    ) -> None:
        """
        Args:
            seed: Semilla para reproducibilidad en backtesting/replay.
            spread_bps: Spread bid-ask en basis points (1 bp = 0.01%).
            volatility_pct: Amplitud del ruido de precio en %.
            simulated_latency_ms: Latencia simulada en ms.
        """
        self._rng = random.Random(seed)
        self._spread_bps = spread_bps
        self._volatility_pct = volatility_pct
        self._simulated_latency_ms = simulated_latency_ms

    async def fetch_snapshot(
        self,
        symbol: str,
        account_balance_usdt: Decimal,
        open_positions_count: int = 0,
        active_orders_count: int = 0,
    ) -> MarketSnapshot:
        now = datetime.now(UTC)
        tick = _TICK_SIZES[symbol]
        base = _BASE_PRICES[symbol]

        last_price = self._noisy_price(base, tick)
        bid, ask = self._bid_ask(last_price, tick)
        spread_absolute = ask - bid
        spread_percent = (spread_absolute / last_price * 100).quantize(
            Decimal("0.0001"), rounding=ROUND_DOWN
        )

        candles = self._build_candles(last_price, tick, symbol)
        funding_rate = self._rng.uniform(-0.0002, 0.0002)
        open_interest = self._noisy_decimal(
            _BASE_OPEN_INTEREST[symbol], noise_pct=1.0, tick=Decimal("1")
        )
        volatility = round(self._rng.uniform(0.005, 0.04), 4)

        return MarketSnapshot(
            timestamp_utc=now,
            exchange=Exchange.PAPER,
            environment=Environment.PAPER,
            symbol=symbol,
            market="FUTURES",
            last_price=last_price,
            bid=bid,
            ask=ask,
            spread_absolute=spread_absolute,
            spread_percent=spread_percent,
            candles=candles,
            volume=self._noisy_decimal(_BASE_VOLUMES[symbol], noise_pct=5.0, tick=Decimal("1")),
            volatility=volatility,
            funding_rate=round(funding_rate, 6),
            open_interest=open_interest,
            account_balance_usdt=account_balance_usdt,
            open_positions_count=open_positions_count,
            active_orders_count=active_orders_count,
            latency_ms=self._simulated_latency_ms,
            exchange_server_time=now,
            local_time=now,
            clock_skew_ms=0,
            data_freshness_status=DataFreshnessStatus.FRESH,
            coherence_status=CoherenceStatus.OK,
        )

    def is_healthy(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _noisy_price(self, base: Decimal, tick: Decimal) -> Decimal:
        noise = self._rng.gauss(0, float(base) * self._volatility_pct / 100)
        raw = float(base) + noise
        return self._round_to_tick(Decimal(str(raw)), tick)

    def _bid_ask(self, last_price: Decimal, tick: Decimal) -> tuple[Decimal, Decimal]:
        half_spread = last_price * Decimal(str(self._spread_bps / 2 / 10000))
        half_spread = max(half_spread, tick)

        bid = self._round_to_tick(last_price - half_spread, tick)
        ask = self._round_to_tick(last_price + half_spread, tick)

        # last_price debe quedar en [bid, ask]; ajustamos si el redondeo lo sacó
        if last_price <= bid:
            bid = last_price - tick
        if last_price >= ask:
            ask = last_price + tick

        return bid, ask

    def _build_candles(self, last_price: Decimal, tick: Decimal, symbol: str) -> Candles:
        base_vol = _BASE_VOLUMES[symbol]
        return Candles(
            tf_5m=self._make_candle(last_price, tick, base_vol, multiplier=1),
            tf_15m=self._make_candle(last_price, tick, base_vol, multiplier=3),
            tf_1h=self._make_candle(last_price, tick, base_vol, multiplier=12),
            tf_4h=self._make_candle(last_price, tick, base_vol, multiplier=48),
        )

    def _make_candle(
        self,
        close: Decimal,
        tick: Decimal,
        base_vol: Decimal,
        multiplier: int,
    ) -> CandleData:
        """Genera una vela coherente (low ≤ open,close ≤ high)."""
        amplitude = float(close) * self._volatility_pct * multiplier / 100

        open_ = self._round_to_tick(
            Decimal(str(float(close) + self._rng.gauss(0, amplitude * 0.5))), tick
        )
        high = self._round_to_tick(
            Decimal(str(max(float(close), float(open_)) + abs(self._rng.gauss(0, amplitude)))),
            tick,
        )
        low = self._round_to_tick(
            Decimal(str(min(float(close), float(open_)) - abs(self._rng.gauss(0, amplitude)))),
            tick,
        )
        low = max(low, tick)

        volume = self._noisy_decimal(base_vol * multiplier, noise_pct=15.0, tick=Decimal("1"))
        return CandleData(
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            n_candles=multiplier,
        )

    def _noisy_decimal(self, base: Decimal, noise_pct: float, tick: Decimal) -> Decimal:
        noise = self._rng.gauss(0, float(base) * noise_pct / 100)
        raw = max(float(base) + noise, float(tick))
        return self._round_to_tick(Decimal(str(raw)), tick)

    @staticmethod
    def _round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
        return (value / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick
