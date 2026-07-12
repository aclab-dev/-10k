"""Unit tests — SlippageModel (backtesting)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.backtesting.slippage_model import SlippageModel

_D = Decimal
_QUANT = _D("0.00000001")


class TestSlippageModelDefaults:
    def setup_method(self) -> None:
        self.model = SlippageModel()

    # ------------------------------------------------------------------
    # MARKET orders — slippage adverso
    # ------------------------------------------------------------------

    def test_market_buy_price_increases(self) -> None:
        """BUY MARKET: fill price > reference (adverso para el comprador)."""
        price = _D("100")
        fill = self.model.apply(price, "BUY", "MARKET")
        assert fill > price

    def test_market_sell_price_decreases(self) -> None:
        """SELL MARKET: fill price < reference (adverso para el vendedor)."""
        price = _D("100")
        fill = self.model.apply(price, "SELL", "MARKET")
        assert fill < price

    def test_market_buy_default_2bps(self) -> None:
        # price × (1 + 2/10000) = 100 × 1.0002 = 100.02
        price = _D("100")
        fill = self.model.apply(price, "BUY", "MARKET")
        assert fill == _D("100.02000000")

    def test_market_sell_default_2bps(self) -> None:
        # price × (1 - 2/10000) = 100 × 0.9998 = 99.98
        price = _D("100")
        fill = self.model.apply(price, "SELL", "MARKET")
        assert fill == _D("99.98000000")

    def test_market_buy_sell_symmetric(self) -> None:
        """El slippage es simétrico: gap BUY == gap SELL en BPS."""
        price = _D("1000")
        buy_fill = self.model.apply(price, "BUY", "MARKET")
        sell_fill = self.model.apply(price, "SELL", "MARKET")
        buy_gap = buy_fill - price
        sell_gap = price - sell_fill
        assert buy_gap == sell_gap

    # ------------------------------------------------------------------
    # STOP orders — idéntico a MARKET
    # ------------------------------------------------------------------

    def test_stop_buy_same_as_market_buy(self) -> None:
        price = _D("500")
        assert self.model.apply(price, "BUY", "STOP") == self.model.apply(price, "BUY", "MARKET")

    def test_stop_sell_same_as_market_sell(self) -> None:
        price = _D("500")
        assert self.model.apply(price, "SELL", "STOP") == self.model.apply(price, "SELL", "MARKET")

    # ------------------------------------------------------------------
    # LIMIT orders — sin slippage
    # ------------------------------------------------------------------

    def test_limit_buy_no_slippage(self) -> None:
        price = _D("100")
        assert self.model.apply(price, "BUY", "LIMIT") == price.quantize(_QUANT)

    def test_limit_sell_no_slippage(self) -> None:
        price = _D("100")
        assert self.model.apply(price, "SELL", "LIMIT") == price.quantize(_QUANT)

    def test_limit_result_quantized(self) -> None:
        price = _D("123.456789")
        fill = self.model.apply(price, "BUY", "LIMIT")
        assert fill == fill.quantize(_QUANT)

    # ------------------------------------------------------------------
    # Cuantización
    # ------------------------------------------------------------------

    def test_result_quantized_to_8_decimals(self) -> None:
        fill = self.model.apply(_D("33333.33"), "BUY", "MARKET")
        assert fill == fill.quantize(_QUANT)


class TestSlippageModelCustomBPS:
    def test_zero_bps_no_slippage_on_market(self) -> None:
        model = SlippageModel(market_bps=_D("0"))
        price = _D("100")
        assert model.apply(price, "BUY", "MARKET") == price.quantize(_QUANT)
        assert model.apply(price, "SELL", "MARKET") == price.quantize(_QUANT)

    def test_10_bps_market_buy(self) -> None:
        # 100 × (1 + 10/10000) = 100 × 1.001 = 100.1
        model = SlippageModel(market_bps=_D("10"))
        fill = model.apply(_D("100"), "BUY", "MARKET")
        assert fill == _D("100.10000000")

    def test_10_bps_market_sell(self) -> None:
        # 100 × (1 - 10/10000) = 100 × 0.999 = 99.9
        model = SlippageModel(market_bps=_D("10"))
        fill = model.apply(_D("100"), "SELL", "MARKET")
        assert fill == _D("99.90000000")

    def test_custom_bps_limit_unaffected(self) -> None:
        """LIMIT siempre fill al precio exacto, sin importar el BPS."""
        model = SlippageModel(market_bps=_D("100"))
        price = _D("200")
        assert model.apply(price, "BUY", "LIMIT") == price.quantize(_QUANT)


class TestSlippageModelValidation:
    def test_invalid_side_raises_value_error(self) -> None:
        model = SlippageModel()
        with pytest.raises(ValueError, match="Unknown side"):
            model.apply(_D("100"), "LONG", "MARKET")  # type: ignore[arg-type]

    def test_invalid_order_type_raises_value_error(self) -> None:
        model = SlippageModel()
        with pytest.raises(ValueError, match="Unknown order_type"):
            model.apply(_D("100"), "BUY", "FOO")  # type: ignore[arg-type]

    def test_error_message_includes_bad_side(self) -> None:
        model = SlippageModel()
        with pytest.raises(ValueError, match="INVALID_SIDE"):
            model.apply(_D("100"), "INVALID_SIDE", "MARKET")  # type: ignore[arg-type]


class TestSlippageModelReproducibility:
    def test_same_inputs_same_output(self) -> None:
        model = SlippageModel()
        price = _D("47823.55")
        assert model.apply(price, "BUY", "MARKET") == model.apply(price, "BUY", "MARKET")

    def test_different_instances_same_output(self) -> None:
        m1 = SlippageModel()
        m2 = SlippageModel()
        price = _D("9999.99")
        assert m1.apply(price, "SELL", "STOP") == m2.apply(price, "SELL", "STOP")
