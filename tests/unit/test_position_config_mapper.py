"""Tests unitarios del mapper decisión aprobada + defaults -> PositionConfig (F14)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.core.config import PositionManagementConfig
from backend.position_manager.config_mapper import build_position_config


def _defaults(
    *,
    trailing_stop_enabled: bool = True,
    trailing_default_mode: str = "PERCENT",
    trailing_default_percent: float = 0.02,
    trailing_default_atr_multiplier: float = 2.5,
) -> PositionManagementConfig:
    return PositionManagementConfig(
        partial_close_enabled_mvp=False,
        partial_close_enabled_future_phase=True,
        break_even_enabled=True,
        trailing_stop_enabled=trailing_stop_enabled,
        trailing_default_mode=trailing_default_mode,
        trailing_default_percent=trailing_default_percent,
        trailing_default_atr_multiplier=trailing_default_atr_multiplier,
    )


class TestBuildPositionConfigSlTp:
    def test_stop_loss_and_take_profit_passthrough(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            defaults=_defaults(),
        )
        assert config.symbol == "BTCUSDT"
        assert config.stop_loss == Decimal("48000")
        assert config.take_profit == Decimal("53000")
        assert config.resolved_trailing_mode is None


class TestBuildPositionConfigTrailingPercent:
    def test_maps_percent_default(self) -> None:
        config = build_position_config(
            symbol="ETHUSDT",
            stop_loss=Decimal("2800"),
            take_profit=Decimal("3400"),
            use_trailing_stop=True,
            defaults=_defaults(trailing_default_mode="PERCENT", trailing_default_percent=0.02),
        )
        assert config.trailing_percent == Decimal("0.02")
        assert config.trailing_atr is None
        assert config.resolved_trailing_mode is not None


class TestBuildPositionConfigTrailingAtr:
    def test_maps_atr_default_with_atr_1h(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=True,
            defaults=_defaults(trailing_default_mode="ATR", trailing_default_atr_multiplier=2.5),
            atr_1h=Decimal("500"),
        )
        assert config.trailing_atr == Decimal("500")
        assert config.trailing_atr_multiplier == Decimal("2.5")

    def test_atr_mode_without_atr_1h_raises(self) -> None:
        with pytest.raises(ValueError, match="requiere atr_1h"):
            build_position_config(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit=Decimal("53000"),
                use_trailing_stop=True,
                defaults=_defaults(trailing_default_mode="ATR"),
                atr_1h=None,
            )


class TestBuildPositionConfigTrailingFixed:
    def test_fixed_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="FIXED"):
            build_position_config(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit=Decimal("53000"),
                use_trailing_stop=True,
                defaults=_defaults(trailing_default_mode="FIXED"),
            )


class TestBuildPositionConfigTrailingDisabled:
    def test_use_trailing_stop_false_skips_trailing(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            defaults=_defaults(trailing_default_mode="ATR"),
            atr_1h=None,
        )
        assert config.resolved_trailing_mode is None

    def test_trailing_stop_enabled_false_skips_trailing_even_if_requested(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=True,
            defaults=_defaults(trailing_stop_enabled=False, trailing_default_mode="ATR"),
            atr_1h=None,
        )
        assert config.resolved_trailing_mode is None
