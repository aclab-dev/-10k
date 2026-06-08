"""Unit tests — PAPER mode fill simulation: FeeModel, SlippageModel, PaperAdapter (F10)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.backtesting.fee_model import FeeModel
from backend.backtesting.slippage_model import SlippageModel
from backend.exchange_adapters.paper_adapter import FillResult, PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderStatus, OrderType


# ---------------------------------------------------------------------------
# FeeModel
# ---------------------------------------------------------------------------
class TestFeeModel:
    def setup_method(self):
        self.model = FeeModel()

    def test_market_order_uses_taker_rate(self):
        fee = self.model.calculate(Decimal("100"), "MARKET")
        assert fee == Decimal("100") * Decimal("0.0005")

    def test_stop_order_uses_taker_rate(self):
        assert self.model.calculate(Decimal("100"), "STOP") == self.model.calculate(
            Decimal("100"), "MARKET"
        )

    def test_limit_order_uses_maker_rate(self):
        fee = self.model.calculate(Decimal("100"), "LIMIT")
        assert fee == Decimal("100") * Decimal("0.0002")

    def test_taker_fee_higher_than_maker(self):
        notional = Decimal("1000")
        assert self.model.calculate(notional, "MARKET") > self.model.calculate(notional, "LIMIT")

    def test_fee_is_quantized_to_8_decimals(self):
        fee = self.model.calculate(Decimal("33.33333333"), "MARKET")
        assert fee == fee.quantize(Decimal("0.00000001"))

    def test_zero_notional_returns_zero(self):
        assert self.model.calculate(Decimal("0"), "MARKET") == Decimal("0")

    def test_unknown_order_type_raises(self):
        with pytest.raises(ValueError, match="order_type"):
            self.model.calculate(Decimal("100"), "FOK")  # type: ignore[arg-type]

    def test_custom_taker_rate(self):
        model = FeeModel(taker_rate=Decimal("0.001"))
        fee = model.calculate(Decimal("100"), "MARKET")
        assert fee == Decimal("100") * Decimal("0.001")

    def test_custom_maker_rate(self):
        model = FeeModel(maker_rate=Decimal("0.0001"))
        fee = model.calculate(Decimal("100"), "LIMIT")
        assert fee == Decimal("100") * Decimal("0.0001")


# ---------------------------------------------------------------------------
# SlippageModel
# ---------------------------------------------------------------------------
class TestSlippageModel:
    def setup_method(self):
        self.model = SlippageModel()

    def test_market_buy_pays_more(self):
        assert self.model.apply(Decimal("100"), "BUY", "MARKET") > Decimal("100")

    def test_market_sell_receives_less(self):
        assert self.model.apply(Decimal("100"), "SELL", "MARKET") < Decimal("100")

    def test_limit_buy_no_slippage(self):
        price = Decimal("97500.12345678")
        assert self.model.apply(price, "BUY", "LIMIT") == price.quantize(Decimal("0.00000001"))

    def test_limit_sell_no_slippage(self):
        price = Decimal("97500.12345678")
        assert self.model.apply(price, "SELL", "LIMIT") == price.quantize(Decimal("0.00000001"))

    def test_stop_buy_has_adverse_slippage(self):
        price = Decimal("1000")
        assert self.model.apply(price, "BUY", "STOP") > price

    def test_stop_sell_has_adverse_slippage(self):
        price = Decimal("1000")
        assert self.model.apply(price, "SELL", "STOP") < price

    def test_market_buy_slippage_is_2_bps(self):
        price = Decimal("10000")
        fill = self.model.apply(price, "BUY", "MARKET")
        expected = (price * (1 + Decimal("2") / Decimal("10000"))).quantize(Decimal("0.00000001"))
        assert fill == expected

    def test_market_sell_slippage_is_2_bps(self):
        price = Decimal("10000")
        fill = self.model.apply(price, "SELL", "MARKET")
        expected = (price * (1 - Decimal("2") / Decimal("10000"))).quantize(Decimal("0.00000001"))
        assert fill == expected

    def test_output_quantized_to_8_decimals(self):
        fill = self.model.apply(Decimal("97000.123456789"), "BUY", "MARKET")
        assert fill == fill.quantize(Decimal("0.00000001"))

    def test_unknown_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            self.model.apply(Decimal("100"), "LONG", "MARKET")  # type: ignore[arg-type]

    def test_unknown_order_type_raises(self):
        with pytest.raises(ValueError, match="order_type"):
            self.model.apply(Decimal("100"), "BUY", "FOK")  # type: ignore[arg-type]

    def test_custom_market_bps(self):
        model = SlippageModel(market_bps=Decimal("5"))
        price = Decimal("10000")
        fill = model.apply(price, "BUY", "MARKET")
        expected = (price * (1 + Decimal("5") / Decimal("10000"))).quantize(Decimal("0.00000001"))
        assert fill == expected


# ---------------------------------------------------------------------------
# FillResult dataclass
# ---------------------------------------------------------------------------
class TestFillResult:
    """Tests for the FillResult frozen dataclass exported from paper_adapter."""

    def _make_fill(self, **kwargs) -> FillResult:
        defaults: dict = {
            "fill_price": Decimal("97000"),
            "filled_quantity": Decimal("0.001"),
            "requested_quantity": Decimal("0.001"),
            "fee_usdt": Decimal("0.04850"),
            "slippage_usdt": Decimal("0.194"),
            "is_partial": False,
        }
        defaults.update(kwargs)
        return FillResult(**defaults)

    def test_fill_result_is_frozen(self):
        fr = self._make_fill()
        with pytest.raises((AttributeError, TypeError)):
            fr.fill_price = Decimal("999")  # type: ignore[misc]

    def test_notional_usdt_equals_price_times_quantity(self):
        fr = self._make_fill(fill_price=Decimal("97000"), filled_quantity=Decimal("0.001"))
        expected = (Decimal("97000") * Decimal("0.001")).quantize(Decimal("0.00000001"))
        assert fr.notional_usdt == expected

    def test_notional_usdt_quantized_to_8_decimals(self):
        fr = self._make_fill(
            fill_price=Decimal("97000.123456789"), filled_quantity=Decimal("0.001")
        )
        assert fr.notional_usdt == fr.notional_usdt.quantize(Decimal("0.00000001"))

    def test_is_partial_false_for_full_fill(self):
        fr = self._make_fill(is_partial=False)
        assert not fr.is_partial


# ---------------------------------------------------------------------------
# PaperAdapter — integration with FeeModel and SlippageModel
# ---------------------------------------------------------------------------
class TestPaperAdapterFillIntegration:
    """Verifies that PaperAdapter.place_order() delegates math to FeeModel/SlippageModel."""

    def setup_method(self):
        self.adapter = PaperAdapter()  # default 2 BPS, 0.05% taker
        self.fee = FeeModel()
        self.slip = SlippageModel()

    def _req(
        self,
        side: OrderSide = OrderSide.BUY,
        order_type: OrderType = OrderType.MARKET,
        qty: Decimal = Decimal("0.001"),
        price: Decimal = Decimal("97000"),
    ) -> OrderRequest:
        return OrderRequest(
            symbol="BTCUSDT",
            side=side,
            order_type=order_type,
            quantity=qty,
            price=price,
        )

    def test_market_buy_fill_price_matches_slippage_model(self):
        price = Decimal("97000")
        result = self.adapter.place_order(self._req(side=OrderSide.BUY, price=price))
        expected = self.slip.apply(price, "BUY", "MARKET")
        assert result.fill_price == expected

    def test_market_sell_fill_price_matches_slippage_model(self):
        price = Decimal("97000")
        result = self.adapter.place_order(self._req(side=OrderSide.SELL, price=price))
        expected = self.slip.apply(price, "SELL", "MARKET")
        assert result.fill_price == expected

    def test_market_buy_fee_matches_fee_model(self):
        qty = Decimal("0.001")
        price = Decimal("97000")
        result = self.adapter.place_order(self._req(qty=qty, price=price))
        assert result.fill_price is not None
        notional = (result.fill_price * qty).quantize(Decimal("0.00000001"))
        expected_fee = self.fee.calculate(notional, "MARKET")
        assert result.fee_usdt == expected_fee

    def test_limit_order_registers_pending_no_fill(self):
        result = self.adapter.place_order(
            self._req(order_type=OrderType.LIMIT, price=Decimal("96000"))
        )
        assert result.status == OrderStatus.PENDING
        assert result.fill_price is None
        assert result.fee_usdt == Decimal("0")

    def test_stop_market_order_registers_pending(self):
        result = self.adapter.place_order(
            self._req(order_type=OrderType.STOP_MARKET, price=Decimal("96000"))
        )
        assert result.status == OrderStatus.PENDING

    def test_long_entry_buy_market_adverse_slippage(self):
        result = self.adapter.place_order(
            self._req(side=OrderSide.BUY, price=Decimal("97000"))
        )
        assert result.fill_price is not None
        assert result.fill_price > Decimal("97000")
        assert result.fee_usdt > Decimal("0")

    def test_short_entry_sell_market_adverse_slippage(self):
        result = self.adapter.place_order(
            self._req(side=OrderSide.SELL, price=Decimal("97000"))
        )
        assert result.fill_price is not None
        assert result.fill_price < Decimal("97000")
        assert result.fee_usdt > Decimal("0")

    def test_fee_on_max_paper_notional_is_reasonable(self):
        # 10 USDT margin × 10x = 100 USDT notional; taker fee ≈ 0.05 USDT
        notional = Decimal("100")
        price = Decimal("97000")
        qty = (notional / price).quantize(Decimal("0.00000001"))
        result = self.adapter.place_order(self._req(qty=qty, price=price))
        assert result.fee_usdt < Decimal("0.1")  # well under 10% of margin

    def test_custom_fee_model_zero_fee(self):
        class ZeroFee(FeeModel):
            def calculate(self, notional_usdt, order_type):
                return Decimal("0")

        adapter = PaperAdapter(fee_model=ZeroFee())
        result = adapter.place_order(self._req(qty=Decimal("1"), price=Decimal("1000")))
        assert result.fee_usdt == Decimal("0")

    def test_custom_slippage_model_zero_slippage(self):
        class ZeroSlip(SlippageModel):
            def apply(self, price, side, order_type):
                return price.quantize(Decimal("0.00000001"))

        price = Decimal("97000")
        adapter = PaperAdapter(slippage_model=ZeroSlip())
        result = adapter.place_order(self._req(price=price))
        assert result.fill_price == price.quantize(Decimal("0.00000001"))
        assert result.slippage_usdt == Decimal("0")
