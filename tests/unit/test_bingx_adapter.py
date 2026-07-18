"""Tests unitarios — BingXAdapter.

Tarjeta [97]: contrato de interfaz (sin llamadas reales).
Tarjeta [98]: métodos de lectura con httpx mockeado.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from backend.core.config import Environment, MarginType
from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.bingx_adapter import BingXAdapter, BingXApiError
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(responses: dict[str, Any]) -> BingXAdapter:
    """Adapter con transport mockeado que devuelve responses por path."""

    def handler(request: httpx.Request) -> httpx.Response:
        for path_fragment, body in responses.items():
            if path_fragment in request.url.path:
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"code": -1, "msg": "not found"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BingXAdapter(api_key="test-key", api_secret="test-secret", http_client=client)


def _order_request() -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
    )


# ---------------------------------------------------------------------------
# Contrato de interfaz (tarjeta [97])
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> BingXAdapter:
    return BingXAdapter(
        api_key="test-key", api_secret="test-secret", environment=Environment.TESTNET
    )


def test_bingx_adapter_is_exchange_adapter(adapter: BingXAdapter) -> None:
    assert isinstance(adapter, ExchangeAdapter)


def test_environment_property_reflects_constructor_arg() -> None:
    adapter = BingXAdapter(api_key="k", api_secret="s", environment=Environment.LIVE)
    assert adapter.environment == Environment.LIVE


def test_place_order_not_implemented(adapter: BingXAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.place_order(_order_request())


def test_cancel_order_not_implemented(adapter: BingXAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.cancel_order("some-client-order-id")


def test_set_leverage_not_implemented(adapter: BingXAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.set_leverage("BTCUSDT", 5)


def test_set_margin_type_not_implemented(adapter: BingXAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.set_margin_type("BTCUSDT", MarginType.ISOLATED)


def test_paper_and_bingx_adapters_share_the_same_contract() -> None:
    """Ambos adapters son intercambiables donde se tipa ExchangeAdapter (compatibilidad F10)."""
    paper = PaperAdapter()
    bingx = BingXAdapter(api_key="k", api_secret="s")
    for a in (paper, bingx):
        assert isinstance(a, ExchangeAdapter)


# ---------------------------------------------------------------------------
# get_account_state — tarjeta [98]
# ---------------------------------------------------------------------------


def test_get_account_state_maps_fields() -> None:
    adapter = _make_adapter(
        {
            "/user/balance": {
                "code": 0,
                "data": {
                    "balance": {
                        "asset": "USDT",
                        "balance": "1000.00",
                        "equity": "1050.00",
                        "unrealizedProfit": "50.00",
                        "realisedProfit": "0.00",
                        "availableMargin": "800.00",
                        "usedMargin": "200.00",
                        "freezedMargin": "0.00",
                    }
                },
            }
        }
    )
    state = adapter.get_account_state()
    assert state.balance_usdt == Decimal("1000.00")
    assert state.equity_usdt == Decimal("1050.00")
    assert state.available_margin_usdt == Decimal("800.00")
    assert state.used_margin_usdt == Decimal("200.00")
    assert state.is_simulated is False


# ---------------------------------------------------------------------------
# get_position — tarjeta [98]
# ---------------------------------------------------------------------------


def test_get_position_long_returns_buy_side() -> None:
    adapter = _make_adapter(
        {
            "/user/positions": {
                "code": 0,
                "data": [
                    {
                        "symbol": "BTC-USDT",
                        "positionSide": "BOTH",
                        "positionAmt": "0.001",
                        "avgPrice": "50000.00",
                        "markPrice": "51000.00",
                        "unrealizedProfit": "1.00",
                        "initialMargin": "50.00",
                        "leverage": 10,
                    }
                ],
            }
        }
    )
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    assert pos.side == OrderSide.BUY
    assert pos.quantity == Decimal("0.001")
    assert pos.entry_price == Decimal("50000.00")
    assert pos.leverage == 10
    assert pos.is_simulated is False


def test_get_position_short_returns_sell_side() -> None:
    adapter = _make_adapter(
        {
            "/user/positions": {
                "code": 0,
                "data": [
                    {
                        "symbol": "BTC-USDT",
                        "positionSide": "BOTH",
                        "positionAmt": "-0.001",
                        "avgPrice": "50000.00",
                        "markPrice": "49000.00",
                        "unrealizedProfit": "1.00",
                        "initialMargin": "50.00",
                        "leverage": 5,
                    }
                ],
            }
        }
    )
    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    assert pos.side == OrderSide.SELL
    assert pos.quantity == Decimal("0.001")


def test_get_position_zero_amt_returns_none() -> None:
    adapter = _make_adapter(
        {
            "/user/positions": {
                "code": 0,
                "data": [
                    {
                        "symbol": "BTC-USDT",
                        "positionSide": "BOTH",
                        "positionAmt": "0",
                        "avgPrice": "0",
                        "markPrice": "50000",
                        "unrealizedProfit": "0",
                        "initialMargin": "0",
                        "leverage": 1,
                    }
                ],
            }
        }
    )
    assert adapter.get_position("BTCUSDT") is None


def test_get_position_empty_list_returns_none() -> None:
    adapter = _make_adapter({"/user/positions": {"code": 0, "data": []}})
    assert adapter.get_position("BTCUSDT") is None


# ---------------------------------------------------------------------------
# get_open_orders — tarjeta [98]
# ---------------------------------------------------------------------------

_OPEN_ORDER = {
    "symbol": "BTC-USDT",
    "orderId": "987654321",
    "clientOrderId": "550e8400-e29b-41d4-a716-446655440000",
    "type": "LIMIT",
    "side": "BUY",
    "positionSide": "BOTH",
    "price": "49000",
    "origQty": "0.001",
    "executedQty": "0",
    "status": "NEW",
    "time": 1699958400000,
    "updateTime": 1699958400000,
}


def test_get_open_orders_returns_pending_order() -> None:
    adapter = _make_adapter({"/trade/openOrders": {"code": 0, "data": {"orders": [_OPEN_ORDER]}}})
    orders = adapter.get_open_orders("BTCUSDT")
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.PENDING
    assert orders[0].symbol == "BTCUSDT"
    assert orders[0].side == OrderSide.BUY
    assert orders[0].is_simulated is False


def test_get_open_orders_populates_symbol_cache() -> None:
    adapter = _make_adapter({"/trade/openOrders": {"code": 0, "data": {"orders": [_OPEN_ORDER]}}})
    adapter.get_open_orders("BTCUSDT")
    assert adapter._order_symbol_cache["550e8400-e29b-41d4-a716-446655440000"] == "BTCUSDT"


def test_get_open_orders_empty_returns_empty_list() -> None:
    adapter = _make_adapter({"/trade/openOrders": {"code": 0, "data": {"orders": []}}})
    assert adapter.get_open_orders("BTCUSDT") == []


# ---------------------------------------------------------------------------
# get_order_status — tarjeta [98]
# ---------------------------------------------------------------------------

_FILLED_ORDER = {
    "symbol": "BTC-USDT",
    "orderId": "111222333",
    "clientOrderId": "550e8400-e29b-41d4-a716-446655440001",
    "type": "MARKET",
    "side": "SELL",
    "positionSide": "BOTH",
    "price": "0",
    "avgPrice": "51000.00",
    "origQty": "0.001",
    "executedQty": "0.001",
    "status": "FILLED",
    "time": 1699958401000,
    "updateTime": 1699958401000,
}


def test_get_order_status_uses_cached_symbol() -> None:
    client_oid = "550e8400-e29b-41d4-a716-446655440001"
    adapter = _make_adapter({"/trade/order": {"code": 0, "data": _FILLED_ORDER}})
    adapter._order_symbol_cache[client_oid] = "BTCUSDT"
    result = adapter.get_order_status(client_oid)
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert result.fill_price == Decimal("51000.00")


def test_get_order_status_returns_none_when_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 100400, "msg": "order not found", "data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    assert adapter.get_order_status("550e8400-e29b-41d4-a716-446655440099") is None


# ---------------------------------------------------------------------------
# BingXApiError
# ---------------------------------------------------------------------------


def test_signed_get_raises_on_nonzero_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 100413, "msg": "Signature verification failed"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    with pytest.raises(BingXApiError, match="100413"):
        adapter.get_account_state()
