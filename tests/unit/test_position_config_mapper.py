"""Tests unitarios del mapper decisión aprobada + defaults -> PositionConfig (F14)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.core.config import PositionManagementConfig
from backend.position_manager.config_mapper import build_position_config


def _defaults(
    *,
    break_even_enabled: bool = True,
    trailing_stop_enabled: bool = True,
    trailing_default_mode: str = "PERCENT",
    trailing_default_percent: float = 0.02,
    trailing_default_atr_multiplier: float = 2.5,
    be_trigger_atr_multiplier: float = 1.0,
    be_sl_offset_percent: float = 0.0015,
) -> PositionManagementConfig:
    return PositionManagementConfig(
        partial_close_enabled_mvp=False,
        partial_close_enabled_future_phase=True,
        break_even_enabled=break_even_enabled,
        trailing_stop_enabled=trailing_stop_enabled,
        trailing_default_mode=trailing_default_mode,
        trailing_default_percent=trailing_default_percent,
        trailing_default_atr_multiplier=trailing_default_atr_multiplier,
        be_trigger_atr_multiplier=be_trigger_atr_multiplier,
        be_sl_offset_percent=be_sl_offset_percent,
    )


class TestBuildPositionConfigSlTp:
    def test_stop_loss_and_take_profit_passthrough(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            move_to_break_even=False,
            defaults=_defaults(),
            entry_price=Decimal("50000"),
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
            move_to_break_even=False,
            defaults=_defaults(trailing_default_mode="PERCENT", trailing_default_percent=0.02),
            entry_price=Decimal("3000"),
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
            move_to_break_even=False,
            defaults=_defaults(trailing_default_mode="ATR", trailing_default_atr_multiplier=2.5),
            entry_price=Decimal("50000"),
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
                move_to_break_even=False,
                defaults=_defaults(trailing_default_mode="ATR"),
                entry_price=Decimal("50000"),
                atr_1h=None,
            )


class TestBuildPositionConfigTrailingFixed:
    def test_fixed_mode_raises(self) -> None:
        """Defensa en profundidad: PositionManagementConfig ya rechaza FIXED al
        construirse (ver test_config.py::test_blocks_fixed_trailing_mode_at_boot),
        pero el mapper también lo guarda por si el modelo se arma sin pasar por el
        validador (ej. model_construct), en vez de mapear trailing en silencio."""
        defaults = PositionManagementConfig.model_construct(
            partial_close_enabled_mvp=False,
            partial_close_enabled_future_phase=True,
            break_even_enabled=True,
            trailing_stop_enabled=True,
            trailing_default_mode="FIXED",
            trailing_default_percent=0.02,
            trailing_default_atr_multiplier=2.5,
            be_trigger_atr_multiplier=1.0,
            be_sl_offset_percent=0.0015,
        )
        with pytest.raises(ValueError, match="FIXED"):
            build_position_config(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit=Decimal("53000"),
                use_trailing_stop=True,
                move_to_break_even=False,
                defaults=defaults,
                entry_price=Decimal("50000"),
            )


class TestBuildPositionConfigTrailingDisabled:
    def test_use_trailing_stop_false_skips_trailing(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            move_to_break_even=False,
            defaults=_defaults(trailing_default_mode="ATR"),
            entry_price=Decimal("50000"),
            atr_1h=None,
        )
        assert config.resolved_trailing_mode is None

    def test_trailing_stop_enabled_false_skips_trailing_even_if_requested(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=True,
            move_to_break_even=False,
            defaults=_defaults(trailing_stop_enabled=False, trailing_default_mode="ATR"),
            entry_price=Decimal("50000"),
            atr_1h=None,
        )
        assert config.resolved_trailing_mode is None


class TestBuildPositionConfigBreakEven:
    def test_maps_be_trigger_delta_from_atr(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            move_to_break_even=True,
            defaults=_defaults(be_trigger_atr_multiplier=1.0),
            entry_price=Decimal("50000"),
            atr_1h=Decimal("500"),
        )
        assert config.be_trigger_delta == Decimal("500")

    def test_multiplier_scales_the_delta(self) -> None:
        config = build_position_config(
            symbol="ETHUSDT",
            stop_loss=Decimal("2800"),
            take_profit=Decimal("3400"),
            use_trailing_stop=False,
            move_to_break_even=True,
            defaults=_defaults(be_trigger_atr_multiplier=1.5),
            entry_price=Decimal("3000"),
            atr_1h=Decimal("40"),
        )
        assert config.be_trigger_delta == Decimal("60")

    def test_move_to_break_even_false_skips_break_even(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            move_to_break_even=False,
            defaults=_defaults(),
            entry_price=Decimal("50000"),
            atr_1h=Decimal("500"),
        )
        assert config.be_trigger_delta is None
        assert config.be_sl_offset == Decimal("0")

    def test_break_even_enabled_false_skips_break_even_even_if_requested(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            move_to_break_even=True,
            defaults=_defaults(break_even_enabled=False),
            entry_price=Decimal("50000"),
            atr_1h=Decimal("500"),
        )
        assert config.be_trigger_delta is None
        assert config.be_sl_offset == Decimal("0")

    def test_break_even_without_atr_1h_raises(self) -> None:
        with pytest.raises(ValueError, match="move_to_break_even requiere atr_1h"):
            build_position_config(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit=Decimal("53000"),
                use_trailing_stop=False,
                move_to_break_even=True,
                defaults=_defaults(),
                entry_price=Decimal("50000"),
                atr_1h=None,
            )

    def test_break_even_and_trailing_atr_coexist(self) -> None:
        """Ambos mecanismos son ATR-based y comparten el mismo snapshot de atr_1h,
        con distancias independientes: BE dispara antes que el trailing."""
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=True,
            move_to_break_even=True,
            defaults=_defaults(
                trailing_default_mode="ATR",
                trailing_default_atr_multiplier=2.5,
                be_trigger_atr_multiplier=1.0,
            ),
            entry_price=Decimal("50000"),
            atr_1h=Decimal("500"),
        )
        assert config.be_trigger_delta == Decimal("500")
        assert config.trailing_atr == Decimal("500")
        assert config.trailing_atr_multiplier == Decimal("2.5")
        assert config.be_trigger_delta < config.trailing_atr * config.trailing_atr_multiplier


class TestBuildPositionConfigBeSlOffset:
    def test_maps_be_sl_offset_from_entry_price_percent(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            move_to_break_even=True,
            defaults=_defaults(be_trigger_atr_multiplier=1.0, be_sl_offset_percent=0.0015),
            entry_price=Decimal("50000"),
            atr_1h=Decimal("500"),
        )
        # entry_price * be_sl_offset_percent = 50000 * 0.0015 = 75, muy por debajo
        # del tope de 0.5 * be_trigger_delta (250): queda sin clampear.
        assert config.be_sl_offset == Decimal("75")

    def test_be_sl_offset_is_zero_when_break_even_inactive(self) -> None:
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("48000"),
            take_profit=Decimal("53000"),
            use_trailing_stop=False,
            move_to_break_even=False,
            defaults=_defaults(be_sl_offset_percent=0.0015),
            entry_price=Decimal("50000"),
        )
        assert config.be_sl_offset == Decimal("0")
        assert config.be_trigger_delta is None

    def test_be_sl_offset_never_violates_invariant_with_config_yaml_defaults(self) -> None:
        """Reproduce los defaults reales de config.yaml (be_trigger_atr_multiplier=1.0,
        be_sl_offset_percent=0.0015) en un rango de entry_price/atr_1h, incluyendo
        regímenes de baja volatilidad relativa donde el porcentaje del entry podría
        superar el ATR si no se clampeara."""
        defaults = _defaults(be_trigger_atr_multiplier=1.0, be_sl_offset_percent=0.0015)
        cases = [
            (Decimal("50000"), Decimal("500")),  # BTC, ATR normal
            (Decimal("2"), Decimal("0.02")),  # XRP, ATR normal
            (Decimal("50000"), Decimal("5")),  # BTC, régimen de volatilidad muy baja
            (Decimal("100000"), Decimal("1")),  # precio alto, ATR mínimo
        ]
        for entry_price, atr_1h in cases:
            config = build_position_config(
                symbol="BTCUSDT",
                stop_loss=Decimal("1"),
                take_profit=Decimal("999999"),
                use_trailing_stop=False,
                move_to_break_even=True,
                defaults=defaults,
                entry_price=entry_price,
                atr_1h=atr_1h,
            )
            assert config.be_trigger_delta is not None
            assert config.be_sl_offset < config.be_trigger_delta

    def test_be_sl_offset_clamped_below_trigger_in_low_volatility_regime(self) -> None:
        """entry_price alto + atr_1h relativamente chico: el porcentaje del entry
        superaría be_trigger_delta sin el clamp de _BE_SL_OFFSET_MAX_FRACTION_OF_TRIGGER."""
        config = build_position_config(
            symbol="BTCUSDT",
            stop_loss=Decimal("1"),
            take_profit=Decimal("999999"),
            use_trailing_stop=False,
            move_to_break_even=True,
            defaults=_defaults(be_trigger_atr_multiplier=1.0, be_sl_offset_percent=0.0015),
            entry_price=Decimal("100000"),
            atr_1h=Decimal("1"),
        )
        # be_trigger_delta = 1, fee_buffer sin clamp = 100000 * 0.0015 = 150 > 1.
        assert config.be_trigger_delta == Decimal("1")
        assert config.be_sl_offset == Decimal("0.5")  # clamp = 0.5 * be_trigger_delta
