"""Unit tests — FeeModel (backtesting)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.backtesting.fee_model import FeeModel

_D = Decimal
_QUANT = _D("0.00000001")


class TestFeeModelDefaults:
    def setup_method(self) -> None:
        self.model = FeeModel()

    def test_market_taker_rate_is_0005(self) -> None:
        # 100 USDT notional × 0.05% = 0.05 USDT
        fee = self.model.calculate(_D("100"), "MARKET")
        assert fee == _D("0.05000000")

    def test_stop_taker_rate_same_as_market(self) -> None:
        fee_market = self.model.calculate(_D("100"), "MARKET")
        fee_stop = self.model.calculate(_D("100"), "STOP")
        assert fee_market == fee_stop

    def test_limit_maker_rate_is_0002(self) -> None:
        # 100 USDT notional × 0.02% = 0.02 USDT
        fee = self.model.calculate(_D("100"), "LIMIT")
        assert fee == _D("0.02000000")

    def test_taker_greater_than_maker_for_same_notional(self) -> None:
        notional = _D("500")
        assert self.model.calculate(notional, "MARKET") > self.model.calculate(notional, "LIMIT")

    def test_result_quantized_to_8_decimals(self) -> None:
        fee = self.model.calculate(_D("33.33333333"), "MARKET")
        # Debe estar cuantizado a 8 decimales
        assert fee == fee.quantize(_QUANT)

    def test_zero_notional_returns_zero(self) -> None:
        fee = self.model.calculate(_D("0"), "MARKET")
        assert fee == _D("0")

    def test_large_notional(self) -> None:
        # 1_000_000 USDT × 0.05% = 500 USDT
        fee = self.model.calculate(_D("1000000"), "MARKET")
        assert fee == _D("500.00000000")


class TestFeeModelCustomRates:
    def test_custom_taker_rate(self) -> None:
        model = FeeModel(taker_rate=_D("0.001"))  # 0.1%
        fee = self.model_or_custom(model, _D("100"), "MARKET")
        assert fee == _D("0.10000000")

    def test_custom_maker_rate(self) -> None:
        model = FeeModel(maker_rate=_D("0.0001"))  # 0.01%
        fee = self.model_or_custom(model, _D("100"), "LIMIT")
        assert fee == _D("0.01000000")

    def test_zero_taker_rate(self) -> None:
        model = FeeModel(taker_rate=_D("0"))
        fee = self.model_or_custom(model, _D("100"), "MARKET")
        assert fee == _D("0")

    def test_zero_maker_rate(self) -> None:
        model = FeeModel(maker_rate=_D("0"))
        fee = self.model_or_custom(model, _D("100"), "LIMIT")
        assert fee == _D("0")

    @staticmethod
    def model_or_custom(model: FeeModel, notional: Decimal, order_type: str) -> Decimal:
        return model.calculate(notional, order_type)  # type: ignore[arg-type]


class TestFeeModelValidation:
    def test_invalid_order_type_raises_value_error(self) -> None:
        model = FeeModel()
        with pytest.raises(ValueError, match="Unknown order_type"):
            model.calculate(_D("100"), "FOO")  # type: ignore[arg-type]

    def test_error_message_includes_order_type(self) -> None:
        model = FeeModel()
        with pytest.raises(ValueError, match="INVALID"):
            model.calculate(_D("100"), "INVALID")  # type: ignore[arg-type]

    def test_error_message_includes_valid_options(self) -> None:
        model = FeeModel()
        with pytest.raises(ValueError, match="LIMIT"):
            model.calculate(_D("100"), "UNKNOWN")  # type: ignore[arg-type]


class TestFeeModelReproducibility:
    def test_same_inputs_produce_same_output(self) -> None:
        model = FeeModel()
        notional = _D("250.75")
        assert model.calculate(notional, "MARKET") == model.calculate(notional, "MARKET")
        assert model.calculate(notional, "LIMIT") == model.calculate(notional, "LIMIT")

    def test_different_instances_produce_same_output(self) -> None:
        m1 = FeeModel()
        m2 = FeeModel()
        notional = _D("1234.56789")
        assert m1.calculate(notional, "STOP") == m2.calculate(notional, "STOP")
