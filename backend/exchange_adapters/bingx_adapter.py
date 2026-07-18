"""BingXAdapter — implementación de ExchangeAdapter contra BingX Futures (USDT-M).

Métodos implementados en esta tarjeta ([98]):
  - get_account_state  →  GET /openApi/swap/v2/user/balance
  - get_position       →  GET /openApi/swap/v2/user/positions
  - get_open_orders    →  GET /openApi/swap/v2/trade/openOrders
  - get_order_status   →  GET /openApi/swap/v2/trade/order

Métodos pendientes ([99]): place_order, cancel_order, set_leverage, set_margin_type.

Notas de la API de BingX (ver docs/bingx_api_reference.md):
- No existe un host de TESTNET separado: TESTNET y LIVE apuntan al mismo host.
- El formato de símbolo de BingX es "BTC-USDT" (con guión), distinto del
  formato interno del proyecto "BTCUSDT" (ALLOWED_SYMBOLS en schemas.py).
- Firma: HMAC-SHA256(api_secret, query_string).hexdigest() + X-BX-APIKEY header.
- En ONE_WAY mode, positionSide=BOTH; el signo de positionAmt indica dirección.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

from backend.core.config import Environment, MarginType
from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import (
    AccountState,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionState,
)
from backend.market_data.schemas import ALLOWED_SYMBOLS

_log = structlog.get_logger(__name__)

_BASE_URL = "https://open-api.bingx.com"

_STATUS_MAP: dict[str, OrderStatus] = {
    "NEW": OrderStatus.PENDING,
    "PENDING": OrderStatus.PENDING,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "FAILED": OrderStatus.FAILED,
    "EXPIRED": OrderStatus.CANCELLED,
}

_ORDER_TYPE_MAP: dict[str, OrderType] = {
    "MARKET": OrderType.MARKET,
    "LIMIT": OrderType.LIMIT,
    "STOP_MARKET": OrderType.STOP_MARKET,
    "TAKE_PROFIT_MARKET": OrderType.TAKE_PROFIT_MARKET,
}


def _to_bingx_symbol(symbol: str) -> str:
    """BTCUSDT → BTC-USDT (todos los símbolos permitidos terminan en USDT)."""
    return f"{symbol[:-4]}-USDT"


def _from_bingx_symbol(symbol: str) -> str:
    """BTC-USDT → BTCUSDT."""
    return symbol.replace("-", "")


class BingXApiError(Exception):
    """Raised when BingX API returns a non-zero error code."""


class BingXAdapter(ExchangeAdapter):
    """Adapter contra BingX Futures (USDT-M). Ver docs/bingx_api_reference.md."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        environment: Environment = Environment.TESTNET,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        # api_secret nunca debe loguearse ni incluirse en excepciones.
        self._api_key = api_key
        self._api_secret = api_secret
        self._environment = environment
        self._http = http_client or httpx.Client(timeout=httpx.Timeout(10.0))
        # clientOrderId → symbol; poblado por get_open_orders para optimizar get_order_status.
        self._order_symbol_cache: dict[str, str] = {}

    @property
    def environment(self) -> Environment:
        return self._environment

    # ------------------------------------------------------------------
    # Read methods — tarjeta [98]
    # ------------------------------------------------------------------

    def get_account_state(self) -> AccountState:
        data: dict[str, Any] = self._signed_get("/openApi/swap/v2/user/balance", {})
        bal = data["balance"]
        return AccountState(
            balance_usdt=Decimal(str(bal["balance"])),
            equity_usdt=Decimal(str(bal["equity"])),
            available_margin_usdt=Decimal(str(bal["availableMargin"])),
            used_margin_usdt=Decimal(str(bal["usedMargin"])),
            is_simulated=False,
        )

    def get_position(self, symbol: str) -> PositionState | None:
        positions: list[dict[str, Any]] = self._signed_get(
            "/openApi/swap/v2/user/positions",
            {"symbol": _to_bingx_symbol(symbol)},
        )
        if not positions:
            return None
        pos = positions[0]
        amt = Decimal(str(pos["positionAmt"]))
        if amt == Decimal("0"):
            return None
        side = OrderSide.BUY if amt > 0 else OrderSide.SELL
        return PositionState(
            symbol=symbol,
            side=side,
            quantity=abs(amt),
            entry_price=Decimal(str(pos["avgPrice"])),
            mark_price=Decimal(str(pos["markPrice"])),
            unrealized_pnl=Decimal(str(pos["unrealizedProfit"])),
            margin_usdt=Decimal(str(pos["initialMargin"])),
            leverage=int(pos["leverage"]),
            is_simulated=False,
        )

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        data: dict[str, Any] = self._signed_get(
            "/openApi/swap/v2/trade/openOrders",
            {"symbol": _to_bingx_symbol(symbol)},
        )
        results: list[OrderResult] = []
        for raw in data.get("orders", []):
            client_oid: str = raw.get("clientOrderId", "")
            if client_oid:
                self._order_symbol_cache[client_oid] = symbol
            results.append(self._parse_order(raw, symbol))
        return results

    def get_order_status(self, client_order_id: str) -> OrderResult | None:
        # Intentar primero con el símbolo en caché, luego el resto.
        cached = self._order_symbol_cache.get(client_order_id)
        candidates: list[str] = [cached] if cached else []
        candidates += [s for s in sorted(ALLOWED_SYMBOLS) if s not in candidates]

        for symbol in candidates:
            try:
                data: dict[str, Any] = self._signed_get(
                    "/openApi/swap/v2/trade/order",
                    {
                        "symbol": _to_bingx_symbol(symbol),
                        "clientOrderID": client_order_id,
                    },
                )
                if data:
                    self._order_symbol_cache[client_order_id] = symbol
                    return self._parse_order(data, symbol)
            except BingXApiError:
                continue
        return None

    # ------------------------------------------------------------------
    # Order methods — tarjeta [99]
    # ------------------------------------------------------------------

    def place_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError("BingXAdapter.place_order: implementado en la tarjeta [99]")

    def cancel_order(self, client_order_id: str) -> bool:
        raise NotImplementedError("BingXAdapter.cancel_order: implementado en la tarjeta [99]")

    def set_leverage(self, symbol: str, leverage: int) -> None:
        raise NotImplementedError("BingXAdapter.set_leverage: implementado en la tarjeta [99]")

    def set_margin_type(self, symbol: str, margin_type: MarginType) -> None:
        raise NotImplementedError("BingXAdapter.set_margin_type: implementado en la tarjeta [99]")

    # ------------------------------------------------------------------
    # HTTP / firma
    # ------------------------------------------------------------------

    def _signed_get(self, path: str, params: dict[str, str]) -> Any:
        """GET autenticado: añade timestamp y firma HMAC-SHA256 al query string."""
        ts = str(int(time.time() * 1000))
        query_parts = [f"{k}={v}" for k, v in params.items()]
        query_parts.append(f"timestamp={ts}")
        query_string = "&".join(query_parts)
        signature = hmac.new(
            self._api_secret.encode(),
            query_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        url = f"{_BASE_URL}{path}?{query_string}&signature={signature}"
        response = self._http.get(url, headers={"X-BX-APIKEY": self._api_key})
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        if body.get("code", -1) != 0:
            raise BingXApiError(f"BingX error {body.get('code')}: {body.get('msg', '')}")
        return body["data"]

    def _parse_order(self, raw: dict[str, Any], symbol: str) -> OrderResult:
        """Mapea un dict de orden BingX → OrderResult."""
        status = _STATUS_MAP.get(raw.get("status", "FAILED"), OrderStatus.FAILED)
        order_type = _ORDER_TYPE_MAP.get(raw.get("type", "MARKET"), OrderType.MARKET)

        avg_price_str: str = raw.get("avgPrice") or raw.get("price") or "0"
        fill_price = Decimal(avg_price_str) if Decimal(avg_price_str) > 0 else None

        ts_ms: int = int(raw.get("time") or raw.get("updateTime") or 0)
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC) if ts_ms else datetime.now(UTC)

        return OrderResult(
            order_id=str(raw.get("orderId", "")),
            client_order_id=str(raw.get("clientOrderId", "")),
            symbol=symbol,
            side=OrderSide(raw["side"]),
            order_type=order_type,
            status=status,
            quantity_requested=Decimal(str(raw.get("origQty", "0"))),
            quantity_filled=Decimal(str(raw.get("executedQty", "0"))),
            fill_price=fill_price,
            fee_usdt=Decimal(str(raw.get("fee", "0") or "0")),
            slippage_usdt=Decimal("0"),
            is_simulated=False,
            timestamp_utc=ts,
        )
