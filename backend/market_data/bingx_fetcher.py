"""BingXDataFetcher — implementación de DataFetcher usando la API pública de BingX.

Fetches klines (5m/15m/1h/4h), ticker, funding rate y open interest de forma
concurrente para construir un MarketSnapshot completo.

Todos los endpoints usados son públicos (no requieren autenticación).
Ver docs/bingx_api_reference.md, sección 5.1 Market Data.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from backend.core.config import Environment
from backend.market_data.fetcher import DataFetcher
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
    MarketSnapshot,
)

_log = structlog.get_logger(__name__)

_BASE_URL = "https://open-api.bingx.com"
_TIMEOUT = httpx.Timeout(10.0)

# Intervalos BingX → campo en Candles, cantidad de velas de 5m que representa
_TIMEFRAMES: list[tuple[str, str, int]] = [
    ("5m", "tf_5m", 1),
    ("15m", "tf_15m", 3),
    ("1h", "tf_1h", 12),
    ("4h", "tf_4h", 48),
]


def _to_bingx_symbol(symbol: str) -> str:
    """BTCUSDT → BTC-USDT."""
    return f"{symbol[:-4]}-USDT"


class BingXDataFetcher(DataFetcher):
    """Fetcher de datos de mercado contra la API pública de BingX Futures."""

    def __init__(
        self,
        environment: Environment = Environment.TESTNET,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._environment = environment
        self._http = http_client or httpx.AsyncClient(timeout=_TIMEOUT)

    async def fetch_snapshot(
        self,
        symbol: str,
        account_balance_usdt: Decimal,
        open_positions_count: int = 0,
        active_orders_count: int = 0,
    ) -> MarketSnapshot:
        bingx_symbol = _to_bingx_symbol(symbol)
        local_before = datetime.now(UTC)

        kline_tasks = [
            self._get(
                "/openApi/swap/v2/quote/klines",
                {"symbol": bingx_symbol, "interval": interval, "limit": "1"},
            )
            for interval, _, _ in _TIMEFRAMES
        ]
        ticker_task = self._get("/openApi/swap/v2/quote/ticker", {"symbol": bingx_symbol})
        funding_task = self._get(
            "/openApi/swap/v2/quote/premiumIndex", {"symbol": bingx_symbol}
        )
        oi_task = self._get("/openApi/swap/v2/quote/openInterest", {"symbol": bingx_symbol})

        results = await asyncio.gather(
            *kline_tasks, ticker_task, funding_task, oi_task
        )

        local_after = datetime.now(UTC)
        latency_ms = int((local_after - local_before).total_seconds() * 1000)

        klines_raw: list[list[Any]] = [results[i] for i in range(len(_TIMEFRAMES))]
        ticker: dict[str, Any] = results[len(_TIMEFRAMES)]
        funding: dict[str, Any] = results[len(_TIMEFRAMES) + 1]
        oi: dict[str, Any] = results[len(_TIMEFRAMES) + 2]

        candle_data: dict[str, CandleData] = {}
        for (_, field, n_candles), raw_klines in zip(_TIMEFRAMES, klines_raw, strict=True):
            candle_data[field] = _parse_candle(raw_klines[0], n_candles)

        candles = Candles(
            tf_5m=candle_data["tf_5m"],
            tf_15m=candle_data["tf_15m"],
            tf_1h=candle_data["tf_1h"],
            tf_4h=candle_data["tf_4h"],
        )

        last_price = Decimal(str(ticker["lastPrice"]))
        bid = Decimal(str(ticker["bidPrice"]))
        ask = Decimal(str(ticker["askPrice"]))
        spread_absolute = ask - bid
        spread_percent = (spread_absolute / bid * 100).quantize(Decimal("0.0001"))

        server_ts_ms: int = int(ticker.get("time", 0))
        server_time = (
            datetime.fromtimestamp(server_ts_ms / 1000.0, tz=UTC)
            if server_ts_ms
            else local_after
        )
        clock_skew_ms = int(
            (server_time - local_before).total_seconds() * 1000
            - latency_ms // 2
        )
        # Clamp dentro del límite del schema (±5000 ms)
        clock_skew_ms = max(-4999, min(4999, clock_skew_ms))

        funding_rate: float | None = None
        if "lastFundingRate" in funding:
            funding_rate = float(funding["lastFundingRate"])

        open_interest: Decimal | None = None
        if "openInterest" in oi:
            open_interest = Decimal(str(oi["openInterest"]))

        volume = Decimal(str(ticker.get("volume", "0")))

        return MarketSnapshot(
            timestamp_utc=local_before,
            exchange=Exchange.BINGX,
            environment=self._environment,
            symbol=symbol,
            market="FUTURES",
            last_price=last_price,
            bid=bid,
            ask=ask,
            spread_absolute=spread_absolute,
            spread_percent=spread_percent,
            candles=candles,
            volume=volume,
            volatility=None,
            funding_rate=funding_rate,
            open_interest=open_interest,
            account_balance_usdt=account_balance_usdt,
            open_positions_count=open_positions_count,
            active_orders_count=active_orders_count,
            latency_ms=latency_ms,
            exchange_server_time=server_time,
            local_time=local_after,
            clock_skew_ms=clock_skew_ms,
            data_freshness_status=DataFreshnessStatus.FRESH,
            coherence_status=CoherenceStatus.OK,
        )

    def is_healthy(self) -> bool:
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0)) as client:
                resp = client.get(f"{_BASE_URL}/openApi/swap/v2/quote/contracts")
                body: dict[str, Any] = resp.json()
                return int(body.get("code", -1)) == 0
        except Exception:
            return False

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        """GET público sin autenticación."""
        response = await self._http.get(f"{_BASE_URL}{path}", params=params)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        if body.get("code", -1) != 0:
            raise RuntimeError(
                f"BingX error {body.get('code')}: {body.get('msg', '')} — {path}"
            )
        return body["data"]


def _parse_candle(raw: list[Any], n_candles: int) -> CandleData:
    """Mapea un array de kline BingX → CandleData.

    Formato BingX: [open_time, open, high, low, close, volume, close_time, ...]
    """
    return CandleData(
        open=Decimal(str(raw[1])),
        high=Decimal(str(raw[2])),
        low=Decimal(str(raw[3])),
        close=Decimal(str(raw[4])),
        volume=Decimal(str(raw[5])),
        n_candles=n_candles,
    )
