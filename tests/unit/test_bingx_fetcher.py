"""Tests unitarios — BingXDataFetcher (tarjeta [98]: klines y market data)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.core.config import Environment
from backend.core.retry import CircuitBreaker, CircuitBreakerConfig
from backend.market_data.bingx_fetcher import BingXDataFetcher
from backend.market_data.fetcher import DataFetcher
from backend.market_data.schemas import CoherenceStatus, DataFreshnessStatus, Exchange

# ---------------------------------------------------------------------------
# Fixtures y helpers
# ---------------------------------------------------------------------------

_KLINE = [
    1699958400000,  # open_time
    "50000.0",  # open
    "51000.0",  # high
    "49500.0",  # low
    "50500.0",  # close
    "100.5",  # volume
    1699958699999,  # close_time
    "5050000.0",  # quote_volume
    1234,  # trades
    "60.3",  # taker_buy_base
    "3025500.0",  # taker_buy_quote
    "0",  # ignore
]

_TICKER = {
    "symbol": "BTC-USDT",
    "lastPrice": "50500.0",
    "bidPrice": "50490.0",
    "askPrice": "50510.0",
    "volume": "100.5",
    "quoteVolume": "5070250.0",
    "time": 1699958400000,
}

_FUNDING = {
    "symbol": "BTC-USDT",
    "markPrice": "50502.0",
    "indexPrice": "50498.0",
    "lastFundingRate": "0.0001",
    "nextFundingTime": 1699966800000,
}

_OI = {
    "openInterest": "12345.678",
    "symbol": "BTC-USDT",
    "time": 1699958400000,
}


def _make_fetcher() -> BingXDataFetcher:
    call_count: dict[str, int] = {"klines": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "klines" in path:
            call_count["klines"] += 1
            return httpx.Response(200, json={"code": 0, "data": [_KLINE]})
        if "ticker" in path:
            return httpx.Response(200, json={"code": 0, "data": _TICKER})
        if "premiumIndex" in path:
            return httpx.Response(200, json={"code": 0, "data": _FUNDING})
        if "openInterest" in path:
            return httpx.Response(200, json={"code": 0, "data": _OI})
        return httpx.Response(404, json={"code": -1, "msg": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BingXDataFetcher(environment=Environment.TESTNET, http_client=client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bingx_fetcher_is_data_fetcher() -> None:
    assert isinstance(BingXDataFetcher(), DataFetcher)


@pytest.mark.asyncio
async def test_fetch_snapshot_returns_market_snapshot() -> None:
    fetcher = _make_fetcher()
    snapshot = await fetcher.fetch_snapshot(
        symbol="BTCUSDT",
        account_balance_usdt=Decimal("1000"),
        open_positions_count=0,
        active_orders_count=0,
    )
    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.exchange == Exchange.BINGX
    assert snapshot.environment == Environment.TESTNET
    assert snapshot.market == "FUTURES"


@pytest.mark.asyncio
async def test_fetch_snapshot_prices_from_ticker() -> None:
    fetcher = _make_fetcher()
    snapshot = await fetcher.fetch_snapshot(symbol="BTCUSDT", account_balance_usdt=Decimal("1000"))
    assert snapshot.last_price == Decimal("50500.0")
    assert snapshot.bid == Decimal("50490.0")
    assert snapshot.ask == Decimal("50510.0")
    assert snapshot.spread_absolute == Decimal("20.0")


@pytest.mark.asyncio
async def test_fetch_snapshot_candles_from_klines() -> None:
    fetcher = _make_fetcher()
    snapshot = await fetcher.fetch_snapshot(symbol="BTCUSDT", account_balance_usdt=Decimal("1000"))
    assert snapshot.candles.tf_5m.open == Decimal("50000.0")
    assert snapshot.candles.tf_5m.high == Decimal("51000.0")
    assert snapshot.candles.tf_5m.low == Decimal("49500.0")
    assert snapshot.candles.tf_5m.close == Decimal("50500.0")
    assert snapshot.candles.tf_5m.volume == Decimal("100.5")
    assert snapshot.candles.tf_5m.n_candles == 1
    assert snapshot.candles.tf_15m.n_candles == 3
    assert snapshot.candles.tf_1h.n_candles == 12
    assert snapshot.candles.tf_4h.n_candles == 48


@pytest.mark.asyncio
async def test_fetch_snapshot_funding_rate() -> None:
    fetcher = _make_fetcher()
    snapshot = await fetcher.fetch_snapshot(symbol="BTCUSDT", account_balance_usdt=Decimal("1000"))
    assert snapshot.funding_rate == pytest.approx(0.0001)


@pytest.mark.asyncio
async def test_fetch_snapshot_open_interest() -> None:
    fetcher = _make_fetcher()
    snapshot = await fetcher.fetch_snapshot(symbol="BTCUSDT", account_balance_usdt=Decimal("1000"))
    assert snapshot.open_interest == Decimal("12345.678")


@pytest.mark.asyncio
async def test_fetch_snapshot_account_fields_passed_through() -> None:
    fetcher = _make_fetcher()
    snapshot = await fetcher.fetch_snapshot(
        symbol="BTCUSDT",
        account_balance_usdt=Decimal("5000"),
        open_positions_count=2,
        active_orders_count=3,
    )
    assert snapshot.account_balance_usdt == Decimal("5000")
    assert snapshot.open_positions_count == 2
    assert snapshot.active_orders_count == 3


@pytest.mark.asyncio
async def test_fetch_snapshot_freshness_and_coherence() -> None:
    fetcher = _make_fetcher()
    snapshot = await fetcher.fetch_snapshot(symbol="BTCUSDT", account_balance_usdt=Decimal("1000"))
    assert snapshot.data_freshness_status == DataFreshnessStatus.FRESH
    assert snapshot.coherence_status == CoherenceStatus.OK


@pytest.mark.asyncio
async def test_fetch_snapshot_all_symbols() -> None:
    """Verifica que el fetcher acepte todos los símbolos permitidos."""
    fetcher = _make_fetcher()
    for symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"):
        snapshot = await fetcher.fetch_snapshot(symbol=symbol, account_balance_usdt=Decimal("1000"))
        assert snapshot.symbol == symbol


# ---------------------------------------------------------------------------
# Retry de transporte y circuit breaker (F16)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_retries_on_timeout_then_succeeds() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.TimeoutException("timed out")
        return httpx.Response(200, json={"code": 0, "data": _TICKER})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = BingXDataFetcher(environment=Environment.TESTNET, http_client=client)

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await fetcher._get("/openApi/swap/v2/quote/ticker", {"symbol": "BTC-USDT"})

    assert result == _TICKER
    assert call_count == 2


@pytest.mark.asyncio
async def test_get_does_not_retry_on_business_error() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"code": 80001, "msg": "invalid symbol"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = BingXDataFetcher(environment=Environment.TESTNET, http_client=client)

    with pytest.raises(RuntimeError, match="80001"):
        await fetcher._get("/openApi/swap/v2/quote/ticker", {"symbol": "BTC-USDT"})

    assert call_count == 1


@pytest.mark.asyncio
async def test_get_circuit_breaker_opens_after_consecutive_failures() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = BingXDataFetcher(environment=Environment.TESTNET, http_client=client)
    fetcher._circuit_breaker = CircuitBreaker(
        CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=60.0)
    )

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError):
            await fetcher._get("/openApi/swap/v2/quote/ticker", {"symbol": "BTC-USDT"})
        calls_after_first = call_count
        with pytest.raises(RuntimeError, match="circuit breaker"):
            await fetcher._get("/openApi/swap/v2/quote/ticker", {"symbol": "BTC-USDT"})

    assert call_count == calls_after_first
