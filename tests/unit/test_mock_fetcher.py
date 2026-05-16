"""Tests del MockDataFetcher y la interfaz DataFetcher."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.core.config import Environment
from backend.market_data.fetcher import DataFetcher, MockDataFetcher
from backend.market_data.schemas import (
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)

_BALANCE = Decimal("500")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch(symbol: str = "BTCUSDT", **kwargs: object) -> MarketSnapshot:
    fetcher = MockDataFetcher(seed=42, **kwargs)  # type: ignore[arg-type]
    return await fetcher.fetch_snapshot(symbol, account_balance_usdt=_BALANCE)


# ---------------------------------------------------------------------------
# Interfaz
# ---------------------------------------------------------------------------


class TestDataFetcherInterface:
    def test_mock_is_data_fetcher(self) -> None:
        assert isinstance(MockDataFetcher(), DataFetcher)

    def test_is_healthy_returns_true(self) -> None:
        assert MockDataFetcher().is_healthy() is True


# ---------------------------------------------------------------------------
# Snapshot válido
# ---------------------------------------------------------------------------


class TestMockFetcherReturnsValidSnapshot:
    async def test_returns_market_snapshot(self) -> None:
        snap = await _fetch()
        assert isinstance(snap, MarketSnapshot)

    async def test_exchange_is_paper(self) -> None:
        snap = await _fetch()
        assert snap.exchange == Exchange.PAPER

    async def test_environment_is_paper(self) -> None:
        snap = await _fetch()
        assert snap.environment == Environment.PAPER

    async def test_freshness_ok(self) -> None:
        snap = await _fetch()
        assert snap.data_freshness_status == DataFreshnessStatus.FRESH
        assert snap.coherence_status == CoherenceStatus.OK

    async def test_timestamp_utc_aware(self) -> None:
        snap = await _fetch()
        assert snap.timestamp_utc.tzinfo is not None

    async def test_account_balance_passed_through(self) -> None:
        snap = await _fetch()
        assert snap.account_balance_usdt == _BALANCE

    @pytest.mark.parametrize(
        "symbol",
        ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"],
    )
    async def test_all_allowed_symbols(self, symbol: str) -> None:
        snap = await _fetch(symbol=symbol)
        assert snap.symbol == symbol

    async def test_positions_and_orders_passed_through(self) -> None:
        fetcher = MockDataFetcher(seed=1)
        snap = await fetcher.fetch_snapshot(
            "ETHUSDT",
            account_balance_usdt=_BALANCE,
            open_positions_count=2,
            active_orders_count=3,
        )
        assert snap.open_positions_count == 2
        assert snap.active_orders_count == 3


# ---------------------------------------------------------------------------
# Coherencia de precios
# ---------------------------------------------------------------------------


class TestMockFetcherPriceCoherence:
    @pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"])
    async def test_bid_less_than_ask(self, symbol: str) -> None:
        for seed in range(10):
            fetcher = MockDataFetcher(seed=seed)
            snap = await fetcher.fetch_snapshot(symbol, account_balance_usdt=_BALANCE)
            assert snap.bid < snap.ask, f"{symbol} seed={seed}: bid={snap.bid} >= ask={snap.ask}"

    @pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"])
    async def test_last_price_in_range(self, symbol: str) -> None:
        for seed in range(10):
            fetcher = MockDataFetcher(seed=seed)
            snap = await fetcher.fetch_snapshot(symbol, account_balance_usdt=_BALANCE)
            assert snap.bid <= snap.last_price <= snap.ask, (
                f"{symbol} seed={seed}: last_price={snap.last_price} "
                f"not in [{snap.bid}, {snap.ask}]"
            )

    async def test_spread_positive(self) -> None:
        snap = await _fetch()
        assert snap.spread_absolute > 0
        assert snap.spread_percent > 0

    async def test_candle_ohlc_coherent(self) -> None:
        snap = await _fetch()
        for tf_name in ("tf_5m", "tf_15m", "tf_1h", "tf_4h"):
            candle = getattr(snap.candles, tf_name)
            assert candle.low <= candle.open <= candle.high, tf_name
            assert candle.low <= candle.close <= candle.high, tf_name
            assert candle.volume >= 0, tf_name

    async def test_latency_within_bounds(self) -> None:
        snap = await _fetch(simulated_latency_ms=50)
        assert snap.latency_ms == 50

    async def test_clock_skew_is_zero(self) -> None:
        snap = await _fetch()
        assert snap.clock_skew_ms == 0

    async def test_volume_positive(self) -> None:
        snap = await _fetch()
        assert snap.volume > 0

    async def test_open_interest_positive(self) -> None:
        snap = await _fetch()
        assert snap.open_interest is not None
        assert snap.open_interest > 0


# ---------------------------------------------------------------------------
# Reproducibilidad con seed
# ---------------------------------------------------------------------------


class TestMockFetcherReproducibility:
    async def test_same_seed_same_price(self) -> None:
        snap1 = await _fetch()
        snap2 = await _fetch()
        assert snap1.last_price == snap2.last_price

    async def test_different_seeds_different_prices(self) -> None:
        fetcher_a = MockDataFetcher(seed=1)
        fetcher_b = MockDataFetcher(seed=2)
        snap_a = await fetcher_a.fetch_snapshot("BTCUSDT", account_balance_usdt=_BALANCE)
        snap_b = await fetcher_b.fetch_snapshot("BTCUSDT", account_balance_usdt=_BALANCE)
        assert snap_a.last_price != snap_b.last_price

    async def test_no_seed_produces_varying_prices(self) -> None:
        prices = set()
        for _ in range(5):
            fetcher = MockDataFetcher()
            snap = await fetcher.fetch_snapshot("BTCUSDT", account_balance_usdt=_BALANCE)
            prices.add(snap.last_price)
        assert len(prices) > 1
