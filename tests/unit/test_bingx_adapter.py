"""Tests unitarios — BingXAdapter (F13.97: contrato de interfaz, sin llamadas reales)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.core.config import Environment, MarginType
from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.bingx_adapter import BingXAdapter
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType


@pytest.fixture
def adapter() -> BingXAdapter:
    return BingXAdapter(
        api_key="test-key", api_secret="test-secret", environment=Environment.TESTNET
    )


def _order_request() -> OrderRequest:
    return OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
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


def test_get_order_status_not_implemented(adapter: BingXAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.get_order_status("some-client-order-id")


def test_get_position_not_implemented(adapter: BingXAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.get_position("BTCUSDT")


def test_get_open_orders_not_implemented(adapter: BingXAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.get_open_orders("BTCUSDT")


def test_get_account_state_not_implemented(adapter: BingXAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.get_account_state()


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
    for adapter in (paper, bingx):
        assert isinstance(adapter, ExchangeAdapter)
