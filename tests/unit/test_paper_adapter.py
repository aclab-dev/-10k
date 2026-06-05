"""Tests unitarios — PaperAdapter y ExchangeAdapter."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.core.config import Environment, MarginType
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import (
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> PaperAdapter:
    return PaperAdapter(
        initial_balance_usdt=Decimal("1000"),
        taker_fee_rate=Decimal("0.0005"),
        slippage_bps=Decimal("5"),
    )


def _market_order(
    symbol: str = "BTCUSDT",
    side: OrderSide = OrderSide.BUY,
    quantity: Decimal = Decimal("0.001"),
    price: Decimal = Decimal("50000"),
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=price,
    )


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def test_environment_is_paper(adapter: PaperAdapter) -> None:
    assert adapter.environment == Environment.PAPER


# ---------------------------------------------------------------------------
# place_order — MARKET BUY
# ---------------------------------------------------------------------------


def test_market_buy_fills_immediately(adapter: PaperAdapter) -> None:
    req = _market_order(side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"))
    result = adapter.place_order(req)

    assert result.status == OrderStatus.FILLED
    assert result.quantity_filled == Decimal("0.001")
    assert result.is_simulated is True
    assert result.fill_price is not None
    # Slippage aumenta precio en BUY
    assert result.fill_price > Decimal("50000")


def test_market_buy_slippage_correct(adapter: PaperAdapter) -> None:
    price = Decimal("50000")
    quantity = Decimal("0.001")
    req = _market_order(side=OrderSide.BUY, price=price, quantity=quantity)
    result = adapter.place_order(req)

    expected_slip_per_unit = price * Decimal("5") / Decimal("10000")
    expected_fill = price + expected_slip_per_unit
    assert result.fill_price == expected_fill

    expected_slippage_usdt = expected_slip_per_unit * quantity
    assert result.slippage_usdt == expected_slippage_usdt


def test_market_buy_fee_correct(adapter: PaperAdapter) -> None:
    price = Decimal("50000")
    quantity = Decimal("0.001")
    req = _market_order(side=OrderSide.BUY, price=price, quantity=quantity)
    result = adapter.place_order(req)

    fill = result.fill_price
    assert fill is not None
    expected_fee = fill * quantity * Decimal("0.0005")
    assert result.fee_usdt == expected_fee


# ---------------------------------------------------------------------------
# place_order — MARKET SELL
# ---------------------------------------------------------------------------


def test_market_sell_fills_immediately(adapter: PaperAdapter) -> None:
    req = _market_order(side=OrderSide.SELL, price=Decimal("50000"), quantity=Decimal("0.001"))
    result = adapter.place_order(req)

    assert result.status == OrderStatus.FILLED
    assert result.fill_price is not None
    # Slippage reduce precio en SELL
    assert result.fill_price < Decimal("50000")


def test_market_sell_slippage_correct(adapter: PaperAdapter) -> None:
    price = Decimal("50000")
    quantity = Decimal("0.001")
    req = _market_order(side=OrderSide.SELL, price=price, quantity=quantity)
    result = adapter.place_order(req)

    expected_slip_per_unit = price * Decimal("5") / Decimal("10000")
    expected_fill = price - expected_slip_per_unit
    assert result.fill_price == expected_fill


# ---------------------------------------------------------------------------
# place_order — LIMIT queda PENDING
# ---------------------------------------------------------------------------


def test_limit_order_registers_as_pending(adapter: PaperAdapter) -> None:
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"),
        price=Decimal("49000"),
    )
    result = adapter.place_order(req)
    assert result.status == OrderStatus.PENDING
    assert result.quantity_filled == Decimal("0")
    assert result.fill_price is None


# ---------------------------------------------------------------------------
# Idempotencia
# ---------------------------------------------------------------------------


def test_same_client_order_id_returns_same_result(adapter: PaperAdapter) -> None:
    req = _market_order()
    result1 = adapter.place_order(req)
    result2 = adapter.place_order(req)

    assert result1 == result2


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


def test_cancel_pending_order_returns_true(adapter: PaperAdapter) -> None:
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"),
        price=Decimal("49000"),
    )
    adapter.place_order(req)
    assert adapter.cancel_order(req.client_order_id) is True

    status = adapter.get_order_status(req.client_order_id)
    assert status is not None
    assert status.status == OrderStatus.CANCELLED


def test_cancel_filled_order_returns_false(adapter: PaperAdapter) -> None:
    req = _market_order()
    adapter.place_order(req)
    assert adapter.cancel_order(req.client_order_id) is False


def test_cancel_nonexistent_order_returns_false(adapter: PaperAdapter) -> None:
    assert adapter.cancel_order("00000000-0000-0000-0000-000000000000") is False


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------


def test_get_order_status_none_for_unknown(adapter: PaperAdapter) -> None:
    assert adapter.get_order_status("00000000-0000-0000-0000-000000000000") is None


def test_get_order_status_returns_result(adapter: PaperAdapter) -> None:
    req = _market_order()
    adapter.place_order(req)
    result = adapter.get_order_status(req.client_order_id)
    assert result is not None
    assert result.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# get_position
# ---------------------------------------------------------------------------


def test_get_position_none_without_orders(adapter: PaperAdapter) -> None:
    assert adapter.get_position("BTCUSDT") is None


def test_get_position_after_market_buy(adapter: PaperAdapter) -> None:
    adapter.set_leverage("BTCUSDT", 5)
    req = _market_order(side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"))
    adapter.place_order(req)

    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    assert pos.symbol == "BTCUSDT"
    assert pos.quantity == Decimal("0.001")
    assert pos.is_simulated is True
    assert pos.leverage == 5


# ---------------------------------------------------------------------------
# get_open_orders
# ---------------------------------------------------------------------------


def test_get_open_orders_returns_pending_only(adapter: PaperAdapter) -> None:
    limit_req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"),
        price=Decimal("49000"),
    )
    market_req = _market_order()
    adapter.place_order(limit_req)
    adapter.place_order(market_req)

    open_orders = adapter.get_open_orders("BTCUSDT")
    assert len(open_orders) == 1
    assert open_orders[0].client_order_id == limit_req.client_order_id


# ---------------------------------------------------------------------------
# get_account_state
# ---------------------------------------------------------------------------


def test_initial_account_state(adapter: PaperAdapter) -> None:
    state = adapter.get_account_state()
    assert state.balance_usdt == Decimal("1000")
    assert state.used_margin_usdt == Decimal("0")
    assert state.is_simulated is True


def test_account_state_after_order_deducts_fee(adapter: PaperAdapter) -> None:
    adapter.set_leverage("BTCUSDT", 10)
    req = _market_order(price=Decimal("50000"), quantity=Decimal("0.001"))
    result = adapter.place_order(req)

    state = adapter.get_account_state()
    assert state.balance_usdt == Decimal("1000") - result.fee_usdt


# ---------------------------------------------------------------------------
# set_leverage
# ---------------------------------------------------------------------------


def test_set_leverage_valid(adapter: PaperAdapter) -> None:
    adapter.set_leverage("BTCUSDT", 10)  # no debe lanzar


def test_set_leverage_over_limit_raises(adapter: PaperAdapter) -> None:
    with pytest.raises(ValueError, match="fuera del rango"):
        adapter.set_leverage("BTCUSDT", 11)


def test_set_leverage_zero_raises(adapter: PaperAdapter) -> None:
    with pytest.raises(ValueError):
        adapter.set_leverage("BTCUSDT", 0)


# ---------------------------------------------------------------------------
# set_margin_type
# ---------------------------------------------------------------------------


def test_set_margin_isolated_ok(adapter: PaperAdapter) -> None:
    adapter.set_margin_type("BTCUSDT", MarginType.ISOLATED)  # no debe lanzar


def test_set_margin_cross_raises(adapter: PaperAdapter) -> None:
    with pytest.raises(ValueError, match="Cross margin"):
        adapter.set_margin_type("BTCUSDT", MarginType.CROSS)


# ---------------------------------------------------------------------------
# market_order sin price lanza
# ---------------------------------------------------------------------------


def test_market_order_without_price_raises(adapter: PaperAdapter) -> None:
    req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        price=None,
    )
    with pytest.raises(ValueError, match="price > 0"):
        adapter.place_order(req)
