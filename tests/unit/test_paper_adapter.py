"""Tests unitarios — PaperAdapter y ExchangeAdapter."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.backtesting.fee_model import FeeModel
from backend.backtesting.slippage_model import SlippageModel
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

# Los tests del adapter usan 5 BPS de slippage y 0.05% de taker fee,
# mismas tasas que tenía el adapter en PR #65 (antes de la refactorización).
_FEE_MODEL = FeeModel(taker_rate=Decimal("0.0005"), maker_rate=Decimal("0.0002"))
_SLIPPAGE_MODEL = SlippageModel(market_bps=Decimal("5"))


@pytest.fixture
def adapter() -> PaperAdapter:
    return PaperAdapter(
        initial_balance_usdt=Decimal("1000"),
        fee_model=_FEE_MODEL,
        slippage_model=_SLIPPAGE_MODEL,
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


# ---------------------------------------------------------------------------
# is_reduce_only — cierre de posición
# ---------------------------------------------------------------------------


def test_reduce_only_closes_position_and_realizes_pnl(adapter: PaperAdapter) -> None:
    """Bug #2 fix: is_reduce_only debe cerrar la posición y realizar PnL."""
    adapter.set_leverage("BTCUSDT", 1)
    # Abrir LONG a 50000
    open_req = _market_order(side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"))
    adapter.place_order(open_req)
    balance_after_open = adapter.get_account_state().balance_usdt

    # Cerrar a 51000 → PnL positivo de (51000-50000)*0.001 = 1 USDT (menos slippage)
    close_req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        price=Decimal("51000"),
        is_reduce_only=True,
    )
    adapter.place_order(close_req)

    # Posición debe estar cerrada
    assert adapter.get_position("BTCUSDT") is None

    # Balance debe haber aumentado (PnL positivo, menos fee de cierre)
    state = adapter.get_account_state()
    assert state.balance_usdt > balance_after_open


def test_reduce_only_deducts_fee_from_balance(adapter: PaperAdapter) -> None:
    """Bug #1 fix: fee siempre se descuenta, incluso en is_reduce_only."""
    adapter.set_leverage("BTCUSDT", 1)
    open_req = _market_order(side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"))
    adapter.place_order(open_req)

    balance_before_close = adapter.get_account_state().balance_usdt

    close_req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
        is_reduce_only=True,
    )
    result = adapter.place_order(close_req)

    # El fee debe haberse descontado del balance (además del PnL)
    assert result.fee_usdt > Decimal("0")
    # PnL de cierre a mismo precio (con slippage inverso): pequeño, negativo.
    # Lo relevante: balance cambió respecto del punto previo (fee + PnL realizados).
    assert adapter.get_account_state().balance_usdt != balance_before_close


def test_reduce_only_partial_close(adapter: PaperAdapter) -> None:
    """Cierre parcial deja el remanente de posición abierta."""
    adapter.set_leverage("BTCUSDT", 1)
    open_req = _market_order(side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.002"))
    adapter.place_order(open_req)

    close_req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.001"),
        price=Decimal("50000"),
        is_reduce_only=True,
    )
    adapter.place_order(close_req)

    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    assert pos.quantity == Decimal("0.001")


# ---------------------------------------------------------------------------
# Netting de posiciones
# ---------------------------------------------------------------------------


def test_two_buys_same_symbol_nets_position(adapter: PaperAdapter) -> None:
    """Bug #3 fix: dos BUY consecutivos deben netear, no sobreescribir."""
    adapter.set_leverage("BTCUSDT", 1)
    req1 = _market_order(side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"))
    req2 = _market_order(side=OrderSide.BUY, price=Decimal("52000"), quantity=Decimal("0.001"))
    adapter.place_order(req1)
    adapter.place_order(req2)

    pos = adapter.get_position("BTCUSDT")
    assert pos is not None
    assert pos.quantity == Decimal("0.002")
    # Entry price debe ser promedio ponderado de fill prices (incluyendo slippage)
    fill1 = Decimal("50000") * (1 + Decimal("5") / Decimal("10000"))
    fill2 = Decimal("52000") * (1 + Decimal("5") / Decimal("10000"))
    expected_entry = (fill1 + fill2) / Decimal("2")
    assert pos.entry_price == expected_entry


# ---------------------------------------------------------------------------
# get_open_orders — aislamiento por símbolo
# ---------------------------------------------------------------------------


def test_get_open_orders_isolates_by_symbol(adapter: PaperAdapter) -> None:
    """get_open_orders solo retorna órdenes del símbolo solicitado."""
    btc_req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"),
        price=Decimal("49000"),
    )
    eth_req = OrderRequest(
        symbol="ETHUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.01"),
        price=Decimal("3000"),
    )
    adapter.place_order(btc_req)
    adapter.place_order(eth_req)

    btc_orders = adapter.get_open_orders("BTCUSDT")
    eth_orders = adapter.get_open_orders("ETHUSDT")

    assert len(btc_orders) == 1
    assert btc_orders[0].symbol == "BTCUSDT"
    assert len(eth_orders) == 1
    assert eth_orders[0].symbol == "ETHUSDT"


# ---------------------------------------------------------------------------
# Over-close (quantity > posición existente)
# ---------------------------------------------------------------------------


def test_over_close_closes_position_completely(adapter: PaperAdapter) -> None:
    """_close_or_reduce_position con quantity > existing.quantity cierra la posición completa."""
    adapter.set_leverage("BTCUSDT", 1)
    open_req = _market_order(side=OrderSide.BUY, price=Decimal("50000"), quantity=Decimal("0.001"))
    adapter.place_order(open_req)

    # Intentar cerrar más de lo que hay abierto
    close_req = OrderRequest(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.005"),  # mayor que la posición de 0.001
        price=Decimal("50000"),
        is_reduce_only=True,
    )
    adapter.place_order(close_req)

    # Posición debe quedar cerrada (no negativa)
    assert adapter.get_position("BTCUSDT") is None


# ---------------------------------------------------------------------------
# apply_funding
# ---------------------------------------------------------------------------


def test_apply_funding_long_pays(adapter: PaperAdapter) -> None:
    """Long con funding rate positivo descuenta del balance."""
    adapter.set_leverage("BTCUSDT", 1)
    adapter.place_order(
        _market_order(side=OrderSide.BUY, quantity=Decimal("1"), price=Decimal("50000"))
    )
    balance_before = adapter.get_account_state().balance_usdt

    payment = adapter.apply_funding(
        "BTCUSDT", funding_rate=Decimal("0.0001"), mark_price=Decimal("50000")
    )

    # payment = 1 * 50000 * 0.0001 = 5
    assert payment == Decimal("5")
    assert adapter.get_account_state().balance_usdt == balance_before - Decimal("5")


def test_apply_funding_short_receives(adapter: PaperAdapter) -> None:
    """Short con funding rate positivo suma al balance."""
    adapter.set_leverage("BTCUSDT", 1)
    adapter.place_order(
        _market_order(side=OrderSide.SELL, quantity=Decimal("1"), price=Decimal("50000"))
    )
    balance_before = adapter.get_account_state().balance_usdt

    payment = adapter.apply_funding(
        "BTCUSDT", funding_rate=Decimal("0.0001"), mark_price=Decimal("50000")
    )

    # short recibe: payment = -5
    assert payment == Decimal("-5")
    assert adapter.get_account_state().balance_usdt == balance_before + Decimal("5")


def test_apply_funding_no_position_returns_zero(adapter: PaperAdapter) -> None:
    """Sin posición abierta, apply_funding retorna 0 y no toca el balance."""
    balance_before = adapter.get_account_state().balance_usdt
    payment = adapter.apply_funding(
        "BTCUSDT", funding_rate=Decimal("0.0001"), mark_price=Decimal("50000")
    )
    assert payment == Decimal("0")
    assert adapter.get_account_state().balance_usdt == balance_before


def test_apply_funding_accumulates(adapter: PaperAdapter) -> None:
    """Múltiples aplicaciones acumulan correctamente en get_funding_paid."""
    adapter.set_leverage("BTCUSDT", 1)
    adapter.place_order(
        _market_order(side=OrderSide.BUY, quantity=Decimal("1"), price=Decimal("50000"))
    )

    adapter.apply_funding("BTCUSDT", funding_rate=Decimal("0.0001"), mark_price=Decimal("50000"))
    adapter.apply_funding("BTCUSDT", funding_rate=Decimal("0.0001"), mark_price=Decimal("50000"))

    # 5 + 5 = 10
    assert adapter.get_funding_paid("BTCUSDT") == Decimal("10")


def test_get_funding_paid_default_zero(adapter: PaperAdapter) -> None:
    """Sin funding aplicado, get_funding_paid retorna 0."""
    assert adapter.get_funding_paid("BTCUSDT") == Decimal("0")


def test_apply_funding_zero_rate_no_balance_change(adapter: PaperAdapter) -> None:
    """Rate de 0 no modifica el balance."""
    adapter.set_leverage("BTCUSDT", 1)
    adapter.place_order(
        _market_order(side=OrderSide.BUY, quantity=Decimal("1"), price=Decimal("50000"))
    )
    balance_before = adapter.get_account_state().balance_usdt

    payment = adapter.apply_funding(
        "BTCUSDT", funding_rate=Decimal("0"), mark_price=Decimal("50000")
    )

    assert payment == Decimal("0")
    assert adapter.get_account_state().balance_usdt == balance_before


def test_apply_funding_balance_goes_negative_does_not_raise(adapter: PaperAdapter) -> None:
    """Balance negativo post-funding no lanza excepción (se permite en PAPER)."""
    tiny_adapter = PaperAdapter(
        initial_balance_usdt=Decimal("1"),
        fee_model=FeeModel(taker_rate=Decimal("0")),
        slippage_model=SlippageModel(market_bps=Decimal("0")),
    )
    tiny_adapter.set_leverage("BTCUSDT", 1)
    tiny_adapter.place_order(
        _market_order(side=OrderSide.BUY, quantity=Decimal("1"), price=Decimal("50000"))
    )
    # rate grande para forzar balance negativo
    payment = tiny_adapter.apply_funding(
        "BTCUSDT", funding_rate=Decimal("0.01"), mark_price=Decimal("50000")
    )
    assert payment == Decimal("500")
    assert tiny_adapter.get_account_state().balance_usdt < Decimal("0")
