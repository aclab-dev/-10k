"""Unit tests — PartialFillModel (backtesting)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.backtesting.partial_fill_model import PartialFillModel


class TestPartialFillModelDefaults:
    def setup_method(self) -> None:
        self.model = PartialFillModel()

    def test_default_fill_ratio_is_one(self) -> None:
        assert self.model.fill_ratio == Decimal("1.0")

    def test_full_fill_by_default(self) -> None:
        qty = Decimal("0.1")
        assert self.model.compute(qty) == qty

    def test_is_partial_false_by_default(self) -> None:
        assert not self.model.is_partial(Decimal("0.5"))

    def test_zero_quantity_returns_zero(self) -> None:
        assert self.model.compute(Decimal("0")) == Decimal("0")


class TestPartialFillModelCustomRatio:
    def test_80_percent_ratio(self) -> None:
        model = PartialFillModel(fill_ratio=Decimal("0.8"))
        filled = model.compute(Decimal("1.0"))
        assert filled == Decimal("0.80000000")

    def test_is_partial_true_when_ratio_lt_one(self) -> None:
        model = PartialFillModel(fill_ratio=Decimal("0.5"))
        assert model.is_partial(Decimal("1.0"))

    def test_is_partial_false_for_zero_quantity(self) -> None:
        model = PartialFillModel(fill_ratio=Decimal("0.5"))
        assert not model.is_partial(Decimal("0"))

    def test_zero_ratio_fills_nothing(self) -> None:
        model = PartialFillModel(fill_ratio=Decimal("0"))
        assert model.compute(Decimal("1.0")) == Decimal("0")

    def test_output_quantized_to_8_decimals(self) -> None:
        model = PartialFillModel(fill_ratio=Decimal("0.333"))
        result = model.compute(Decimal("1.0"))
        assert result == result.quantize(Decimal("0.00000001"))

    def test_filled_quantity_never_exceeds_requested(self) -> None:
        model = PartialFillModel(fill_ratio=Decimal("0.75"))
        qty = Decimal("2.5")
        assert model.compute(qty) <= qty


class TestPartialFillModelValidation:
    def test_ratio_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="fill_ratio"):
            PartialFillModel(fill_ratio=Decimal("1.1"))

    def test_negative_ratio_raises(self) -> None:
        with pytest.raises(ValueError, match="fill_ratio"):
            PartialFillModel(fill_ratio=Decimal("-0.1"))

    def test_negative_quantity_raises(self) -> None:
        model = PartialFillModel()
        with pytest.raises(ValueError, match="requested_quantity"):
            model.compute(Decimal("-1"))
