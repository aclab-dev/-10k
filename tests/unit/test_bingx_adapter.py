"""Tests unitarios — BingXAdapter.

Tarjeta [97]: contrato de interfaz (sin llamadas reales).
Tarjeta [98]: métodos de lectura con httpx mockeado.
Tarjeta [99]: métodos de escritura (place_order, cancel_order, set_leverage,
set_margin_type) con httpx mockeado.
Tarjeta [100]: idempotencia con clientOrderId UUID.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

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


def _order_request(
    *,
    client_order_id: str | None = None,
    order_type: OrderType = OrderType.MARKET,
    price: Decimal | None = Decimal("50000"),
    stop_price: Decimal | None = None,
) -> OrderRequest:
    kwargs: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "side": OrderSide.BUY,
        "order_type": order_type,
        "quantity": Decimal("0.001"),
        "price": price,
        "stop_price": stop_price,
    }
    if client_order_id is not None:
        kwargs["client_order_id"] = client_order_id
    return OrderRequest(**kwargs)


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


def test_base_url_selected_by_environment() -> None:
    """LIVE opera contra producción; TESTNET/PAPER contra el host VST/demo (tarjeta [101])."""
    live = BingXAdapter(api_key="k", api_secret="s", environment=Environment.LIVE)
    testnet = BingXAdapter(api_key="k", api_secret="s", environment=Environment.TESTNET)
    paper = BingXAdapter(api_key="k", api_secret="s", environment=Environment.PAPER)
    assert live._base_url == "https://open-api.bingx.com"
    assert testnet._base_url == "https://open-api-vst.bingx.com"
    assert paper._base_url == "https://open-api-vst.bingx.com"


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


def test_get_order_status_unwraps_nested_order_key() -> None:
    """GET /trade/order real devuelve la orden anidada bajo data.order — el adapter
    debe desanidarla antes de parsear (tarjeta [101], si no falla con KeyError)."""
    client_oid = _OPEN_ORDER["clientOrderId"]

    def handler(request: httpx.Request) -> httpx.Response:
        # Formato real de BingX: la orden viene envuelta en {"order": {...}}.
        return httpx.Response(200, json={"code": 0, "data": {"order": _OPEN_ORDER}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    result = adapter.get_order_status(client_oid)
    assert result is not None
    assert result.client_order_id == client_oid
    assert result.status == OrderStatus.PENDING


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


def test_get_order_status_falls_through_on_api_error_to_matching_symbol() -> None:
    """Primer símbolo devuelve BingXApiError (orden no existe allí), el segundo acierta."""
    client_oid = "550e8400-e29b-41d4-a716-446655440002"
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json={"code": 100400, "msg": "order not found", "data": {}})
        return httpx.Response(200, json={"code": 0, "data": _FILLED_ORDER})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    result = adapter.get_order_status(client_oid)
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert call_count == 2


def test_get_order_status_continues_after_http_error() -> None:
    """HTTPStatusError en primer símbolo no revienta la consulta; prueba el siguiente."""
    client_oid = "550e8400-e29b-41d4-a716-446655440003"
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"code": 0, "data": _FILLED_ORDER})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    result = adapter.get_order_status(client_oid)
    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert call_count == 2


def test_parse_order_logs_warning_on_unknown_type() -> None:
    """Tipo desconocido de BingX produce OrderType.MARKET sin lanzar excepción."""
    adapter = _make_adapter(
        {
            "/trade/order": {
                "code": 0,
                "data": {**_FILLED_ORDER, "type": "STOP"},
            }
        }
    )
    adapter._order_symbol_cache["550e8400-e29b-41d4-a716-446655440001"] = "BTCUSDT"
    result = adapter.get_order_status("550e8400-e29b-41d4-a716-446655440001")
    assert result is not None
    assert result.order_type == OrderType.MARKET


# ---------------------------------------------------------------------------
# place_order — tarjeta [99]
# ---------------------------------------------------------------------------

_PLACED_MARKET_ORDER = {
    "symbol": "BTC-USDT",
    "orderId": "555000111",
    "clientOrderId": "650e8400-e29b-41d4-a716-446655440010",
    "type": "MARKET",
    "side": "BUY",
    "positionSide": "BOTH",
    "price": "0",
    "avgPrice": "50000.00",
    "origQty": "0.001",
    "executedQty": "0.001",
    "status": "FILLED",
    "time": 1700000000000,
    "updateTime": 1700000000000,
}


def _adapter_with_calls() -> tuple[BingXAdapter, list[httpx.Request]]:
    """Adapter con transport mockeado que registra cada request para inspección."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "/trade/order" in request.url.path and request.method == "GET":
            return httpx.Response(200, json={"code": 100400, "msg": "order not found", "data": {}})
        if "/trade/order" in request.url.path and request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"order": _PLACED_MARKET_ORDER}})
        account_setup_paths = ("v1/positionSide/dual", "/trade/marginType")
        if any(p in request.url.path for p in account_setup_paths):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404, json={"code": -1, "msg": "unexpected call"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    return adapter, calls


def _order_post_calls(calls: list[httpx.Request]) -> list[httpx.Request]:
    return [c for c in calls if c.method == "POST" and "/trade/order" in c.url.path]


def test_place_order_market_success() -> None:
    adapter, calls = _adapter_with_calls()
    request = _order_request(client_order_id="650e8400-e29b-41d4-a716-446655440010")
    result = adapter.place_order(request)
    assert result.status == OrderStatus.FILLED
    assert result.fill_price == Decimal("50000.00")
    assert result.symbol == "BTCUSDT"

    post_calls = _order_post_calls(calls)
    assert len(post_calls) == 1
    assert "positionSide=BOTH" in str(post_calls[0].url)


def test_parse_order_reads_uppercase_client_order_id_key() -> None:
    """La API real de BingX devuelve 'clientOrderID' (ID mayúscula), no 'clientOrderId'
    — _parse_order debe leer ambas casings para no perder el id (tarjeta [101])."""
    client_oid = "650e8400-e29b-41d4-a716-446655440099"
    real_response_order = {
        "orderId": 1234567890,
        "clientOrderID": client_oid,  # casing real de BingX
        "symbol": "BTC-USDT",
        "side": "BUY",
        "type": "LIMIT",
        "status": "NEW",
        "origQty": "0.001",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/trade/order" in request.url.path and request.method == "GET":
            return httpx.Response(200, json={"code": 100400, "msg": "order not found", "data": {}})
        if "/trade/order" in request.url.path and request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"order": real_response_order}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    result = adapter.place_order(_order_request(client_order_id=client_oid))
    assert result.client_order_id == client_oid


def test_place_order_forces_one_way_and_isolated_before_first_order() -> None:
    adapter, calls = _adapter_with_calls()
    adapter.place_order(_order_request(client_order_id="650e8400-e29b-41d4-a716-446655440010"))

    dual_calls = [c for c in calls if "v1/positionSide/dual" in c.url.path]
    margin_calls = [c for c in calls if "/trade/marginType" in c.url.path]
    assert len(dual_calls) == 1
    assert "dualSidePosition=false" in str(dual_calls[0].url)
    assert len(margin_calls) == 1
    assert "marginType=ISOLATED" in str(margin_calls[0].url)

    # Una segunda orden en el mismo symbol no repite la verificación (cacheada).
    adapter.place_order(_order_request(client_order_id="650e8400-e29b-41d4-a716-446655440011"))
    assert len([c for c in calls if "v1/positionSide/dual" in c.url.path]) == 1
    assert len([c for c in calls if "/trade/marginType" in c.url.path]) == 1


def test_place_order_aborts_when_one_way_mode_forcing_fails() -> None:
    """Fail-closed: si falla forzar ONE_WAY, no se intenta colocar la orden."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "v1/positionSide/dual" in request.url.path:
            return httpx.Response(200, json={"code": 100413, "msg": "signature error"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)

    with pytest.raises(BingXApiError):
        adapter.place_order(_order_request(client_order_id="650e8400-e29b-41d4-a716-446655440010"))

    assert not _order_post_calls(calls)
    assert adapter._one_way_verified is False


def test_place_order_aborts_when_isolated_margin_forcing_fails() -> None:
    """Fail-closed: si falla forzar ISOLATED, no se intenta colocar la orden."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "/trade/marginType" in request.url.path:
            return httpx.Response(200, json={"code": 100413, "msg": "signature error"})
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)

    with pytest.raises(BingXApiError):
        adapter.place_order(_order_request(client_order_id="650e8400-e29b-41d4-a716-446655440010"))

    assert not _order_post_calls(calls)
    assert "BTCUSDT" not in adapter._isolated_verified_symbols


def test_place_order_limit_includes_price() -> None:
    adapter, calls = _adapter_with_calls()
    adapter.place_order(
        _order_request(
            client_order_id="650e8400-e29b-41d4-a716-446655440010",
            order_type=OrderType.LIMIT,
            price=Decimal("49000"),
        )
    )
    post_calls = _order_post_calls(calls)
    assert len(post_calls) == 1
    assert "price=49000" in str(post_calls[0].url)
    assert "type=LIMIT" in str(post_calls[0].url)


def test_place_order_stop_market_includes_stop_price() -> None:
    adapter, calls = _adapter_with_calls()
    adapter.place_order(
        _order_request(
            client_order_id="650e8400-e29b-41d4-a716-446655440010",
            order_type=OrderType.STOP_MARKET,
            price=None,
            stop_price=Decimal("48000"),
        )
    )
    post_calls = _order_post_calls(calls)
    assert len(post_calls) == 1
    assert "stopPrice=48000" in str(post_calls[0].url)
    assert "type=STOP_MARKET" in str(post_calls[0].url)


def test_place_order_is_idempotent_by_client_order_id() -> None:
    """Si ya existe una orden con ese client_order_id, no se crea una nueva (POST)."""
    client_oid = "650e8400-e29b-41d4-a716-446655440010"
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": _PLACED_MARKET_ORDER})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)

    result = adapter.place_order(_order_request(client_order_id=client_oid))
    assert result.client_order_id == client_oid
    assert not _order_post_calls(calls), "no debe haber POST a /trade/order si la orden ya existe"


# ---------------------------------------------------------------------------
# cancel_order — tarjeta [99]
# ---------------------------------------------------------------------------


def test_cancel_order_returns_true_when_pending() -> None:
    client_oid = _OPEN_ORDER["clientOrderId"]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": _OPEN_ORDER})
        if request.method == "DELETE":
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404, json={"code": -1, "msg": "unexpected"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    adapter._order_symbol_cache[client_oid] = "BTCUSDT"

    assert adapter.cancel_order(client_oid) is True
    assert any(c.method == "DELETE" for c in calls)


def test_cancel_order_returns_false_when_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 100400, "msg": "order not found", "data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    assert adapter.cancel_order("nonexistent-id") is False


def test_cancel_order_returns_false_when_already_filled() -> None:
    client_oid = _FILLED_ORDER["clientOrderId"]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": _FILLED_ORDER})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    adapter._order_symbol_cache[client_oid] = "BTCUSDT"

    assert adapter.cancel_order(client_oid) is False
    assert all(c.method != "DELETE" for c in calls), "orden ya FILLED no debe intentar cancelarse"


def test_cancel_order_returns_false_when_delete_fails() -> None:
    client_oid = _OPEN_ORDER["clientOrderId"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": _OPEN_ORDER})
        return httpx.Response(200, json={"code": 100400, "msg": "order does not exist"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    adapter._order_symbol_cache[client_oid] = "BTCUSDT"

    assert adapter.cancel_order(client_oid) is False


def test_cancel_order_returns_true_when_partially_filled() -> None:
    partially_filled_order = {**_OPEN_ORDER, "status": "PARTIALLY_FILLED", "executedQty": "0.0004"}
    client_oid = partially_filled_order["clientOrderId"]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": partially_filled_order})
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)
    adapter._order_symbol_cache[client_oid] = "BTCUSDT"

    assert adapter.cancel_order(client_oid) is True
    assert any(c.method == "DELETE" for c in calls)


# ---------------------------------------------------------------------------
# set_leverage — tarjeta [99]
# ---------------------------------------------------------------------------


def test_set_leverage_success_within_cap() -> None:
    adapter = _make_adapter({"/trade/leverage": {"code": 0, "data": {}}})
    adapter.set_leverage("BTCUSDT", 5)  # TESTNET (default) cap = 5x


def test_set_leverage_raises_when_exceeds_testnet_cap() -> None:
    adapter = BingXAdapter(api_key="k", api_secret="s", environment=Environment.TESTNET)
    with pytest.raises(ValueError, match="TESTNET"):
        adapter.set_leverage("BTCUSDT", 6)


def test_set_leverage_raises_when_exceeds_paper_cap() -> None:
    adapter = BingXAdapter(api_key="k", api_secret="s", environment=Environment.PAPER)
    with pytest.raises(ValueError, match="PAPER"):
        adapter.set_leverage("BTCUSDT", 11)


def test_set_leverage_live_cap_is_five() -> None:
    """LIVE absoluto: 5x (el tope de 3x inicial es operativo, no de esta clase)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {}})

    adapter = BingXAdapter(
        api_key="k",
        api_secret="s",
        environment=Environment.LIVE,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    adapter.set_leverage("BTCUSDT", 5)
    with pytest.raises(ValueError, match="LIVE"):
        adapter.set_leverage("BTCUSDT", 6)


# ---------------------------------------------------------------------------
# set_margin_type — tarjeta [99]
# ---------------------------------------------------------------------------


def test_set_margin_type_success_sets_isolated() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)

    adapter.set_margin_type("BTCUSDT", MarginType.ISOLATED)
    assert len(calls) == 1
    assert "marginType=ISOLATED" in str(calls[0].url)


def test_set_margin_type_raises_on_cross() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debe llamar a la API si margin_type=CROSS")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)

    with pytest.raises(ValueError, match="Cross margin"):
        adapter.set_margin_type("BTCUSDT", MarginType.CROSS)


# ---------------------------------------------------------------------------
# clientOrderId idempotencia — tarjeta [100]
# ---------------------------------------------------------------------------


def test_order_request_auto_generates_uuid_client_order_id() -> None:
    """Si no se pasa client_order_id, OrderRequest genera un UUID v4 válido."""
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
    )
    uuid.UUID(req.client_order_id)  # lanza ValueError si no es UUID válido


def test_order_request_rejects_non_uuid_client_order_id() -> None:
    """Un client_order_id que no sea UUID válido se rechaza al construir el request."""
    with pytest.raises(ValidationError):
        OrderRequest(
            client_order_id="not-a-uuid",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.001"),
        )


def test_place_order_sends_client_order_id_in_api_request() -> None:
    """El clientOrderID del request aparece en la URL del POST enviado a BingX."""
    client_oid = "650e8400-e29b-41d4-a716-446655440099"
    adapter, calls = _adapter_with_calls()
    adapter.place_order(_order_request(client_order_id=client_oid))
    post_calls = _order_post_calls(calls)
    assert len(post_calls) == 1
    assert client_oid in str(post_calls[0].url)


def test_place_order_aborts_on_http_error_during_idempotency_check() -> None:
    """HTTP error (ej. 429) en la consulta previa aborta place_order sin hacer POST."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        setup_paths = ("v1/positionSide/dual", "/trade/marginType")
        if any(p in request.url.path for p in setup_paths):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(429, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = BingXAdapter(api_key="k", api_secret="s", http_client=client)

    with pytest.raises(httpx.HTTPStatusError):
        adapter.place_order(_order_request(client_order_id="650e8400-e29b-41d4-a716-446655440010"))

    assert not _order_post_calls(calls), "no debe POST cuando el GET de idempotencia falla"
