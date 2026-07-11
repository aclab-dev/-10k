"""Unit tests — LatencyModel (backtesting)."""

from __future__ import annotations

import pytest

from backend.backtesting.latency_model import LatencyModel


class TestLatencyModelDefaults:
    def test_default_extra_candles_is_zero(self) -> None:
        model = LatencyModel()
        assert model.extra_candles == 0

    def test_default_fill_offset_is_one(self) -> None:
        """Sin latencia extra, el fill ocurre en el candle siguiente (offset=1)."""
        model = LatencyModel()
        assert model.fill_candle_offset() == 1

    def test_extra_candles_zero_explicit(self) -> None:
        model = LatencyModel(extra_candles=0)
        assert model.fill_candle_offset() == 1


class TestLatencyModelCustom:
    def test_extra_one_candle_offset_is_two(self) -> None:
        model = LatencyModel(extra_candles=1)
        assert model.fill_candle_offset() == 2

    def test_extra_five_candles_offset_is_six(self) -> None:
        model = LatencyModel(extra_candles=5)
        assert model.fill_candle_offset() == 6

    def test_fill_offset_equals_one_plus_extra(self) -> None:
        for extra in range(10):
            assert LatencyModel(extra_candles=extra).fill_candle_offset() == 1 + extra


class TestLatencyModelValidation:
    def test_negative_extra_candles_raises(self) -> None:
        with pytest.raises(ValueError, match="negativo"):
            LatencyModel(extra_candles=-1)

    def test_negative_large_raises(self) -> None:
        with pytest.raises(ValueError):
            LatencyModel(extra_candles=-100)


class TestLatencyModelImmutability:
    def test_is_frozen_dataclass(self) -> None:
        model = LatencyModel(extra_candles=2)
        with pytest.raises((AttributeError, TypeError)):
            model.extra_candles = 5  # type: ignore[misc]

    def test_same_config_equal(self) -> None:
        assert LatencyModel(extra_candles=3) == LatencyModel(extra_candles=3)

    def test_different_config_not_equal(self) -> None:
        assert LatencyModel(extra_candles=1) != LatencyModel(extra_candles=2)
