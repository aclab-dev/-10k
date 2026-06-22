"""Unit tests — LatencyModel (backtesting)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.backtesting.latency_model import LatencyModel


class TestLatencyModelDefaults:
    def setup_method(self) -> None:
        self.model = LatencyModel()

    def test_default_latency_ms(self) -> None:
        assert self.model.latency_ms == 50

    def test_default_bps_per_ms(self) -> None:
        assert self.model.bps_per_ms == Decimal("0.002")

    def test_buy_price_increases(self) -> None:
        assert self.model.apply(Decimal("10000"), "BUY") > Decimal("10000")

    def test_sell_price_decreases(self) -> None:
        assert self.model.apply(Decimal("10000"), "SELL") < Decimal("10000")

    def test_output_quantized_to_8_decimals(self) -> None:
        result = self.model.apply(Decimal("97000.123456789"), "BUY")
        assert result == result.quantize(Decimal("0.00000001"))

    def test_buy_adverse_is_symmetric_to_sell(self) -> None:
        price = Decimal("10000")
        buy_impact = self.model.apply(price, "BUY") - price
        sell_impact = price - self.model.apply(price, "SELL")
        assert buy_impact == sell_impact


class TestLatencyModelZeroLatency:
    def test_zero_latency_buy_returns_price_unchanged(self) -> None:
        model = LatencyModel(latency_ms=0)
        price = Decimal("97000")
        assert model.apply(price, "BUY") == price.quantize(Decimal("0.00000001"))

    def test_zero_latency_sell_returns_price_unchanged(self) -> None:
        model = LatencyModel(latency_ms=0)
        price = Decimal("97000")
        assert model.apply(price, "SELL") == price.quantize(Decimal("0.00000001"))


class TestLatencyModelCustomParams:
    def test_custom_latency_buy_price_computation(self) -> None:
        model = LatencyModel(latency_ms=100, bps_per_ms=Decimal("0.001"))
        price = Decimal("10000")
        # total_bps = 0.001 * 100 = 0.1 BPS; factor = 0.1 / 10000 = 0.00001
        expected = (price * (1 + Decimal("0.001") * 100 / Decimal("10000"))).quantize(
            Decimal("0.00000001")
        )
        assert model.apply(price, "BUY") == expected

    def test_higher_latency_means_more_adverse_impact(self) -> None:
        price = Decimal("10000")
        low = LatencyModel(latency_ms=10)
        high = LatencyModel(latency_ms=200)
        assert high.apply(price, "BUY") > low.apply(price, "BUY")

    def test_higher_bps_per_ms_means_more_adverse_impact(self) -> None:
        price = Decimal("10000")
        low = LatencyModel(latency_ms=50, bps_per_ms=Decimal("0.001"))
        high = LatencyModel(latency_ms=50, bps_per_ms=Decimal("0.01"))
        assert high.apply(price, "BUY") > low.apply(price, "BUY")


class TestLatencyModelCost:
    def test_latency_cost_zero_when_latency_is_zero(self) -> None:
        model = LatencyModel(latency_ms=0)
        cost = model.latency_cost_usdt(Decimal("10000"), Decimal("1"), "BUY")
        assert cost == Decimal("0")

    def test_latency_cost_positive_for_buy(self) -> None:
        model = LatencyModel(latency_ms=50)
        cost = model.latency_cost_usdt(Decimal("10000"), Decimal("1"), "BUY")
        assert cost > Decimal("0")

    def test_latency_cost_positive_for_sell(self) -> None:
        model = LatencyModel(latency_ms=50)
        cost = model.latency_cost_usdt(Decimal("10000"), Decimal("1"), "SELL")
        assert cost > Decimal("0")

    def test_latency_cost_scales_with_quantity(self) -> None:
        model = LatencyModel(latency_ms=50)
        price = Decimal("10000")
        c1 = model.latency_cost_usdt(price, Decimal("1"), "BUY")
        c2 = model.latency_cost_usdt(price, Decimal("2"), "BUY")
        assert c2 == c1 * 2


class TestLatencyModelValidation:
    def test_unknown_side_raises(self) -> None:
        model = LatencyModel()
        with pytest.raises(ValueError, match="side"):
            model.apply(Decimal("10000"), "LONG")  # type: ignore[arg-type]

    def test_negative_latency_ms_raises(self) -> None:
        with pytest.raises(ValueError, match="latency_ms"):
            LatencyModel(latency_ms=-1)

    def test_negative_bps_per_ms_raises(self) -> None:
        with pytest.raises(ValueError, match="bps_per_ms"):
            LatencyModel(bps_per_ms=Decimal("-0.001"))
