"""Tests unitarios del PositionManager (SL/TP/trailing stop)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.position_manager import (
    InvalidationAction,
    PositionConfig,
    PositionManager,
    PositionTriggerReason,
    TakeProfitLevel,
    TrailingMode,
)
from backend.position_manager.break_even import maybe_move_to_break_even
from backend.position_manager.trailing import (
    compute_trailing_stop,
    is_trailing_stop_hit,
    resolve_trailing_delta,
    trailing_stop_from_delta,
    update_high_water,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_long(adapter: PaperAdapter, symbol: str, qty: Decimal, price: Decimal) -> None:
    adapter.set_leverage(symbol, 1)
    adapter.place_order(
        OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=qty,
            price=price,
        )
    )


def _open_short(adapter: PaperAdapter, symbol: str, qty: Decimal, price: Decimal) -> None:
    adapter.set_leverage(symbol, 1)
    adapter.place_order(
        OrderRequest(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=qty,
            price=price,
        )
    )


# ---------------------------------------------------------------------------
# compute_trailing_stop (función pura)
# ---------------------------------------------------------------------------


class TestComputeTrailingStop:
    def test_long_first_tick_sets_high_water(self) -> None:
        hw, stop = compute_trailing_stop(OrderSide.BUY, Decimal("100"), Decimal("5"), None)
        assert hw == Decimal("100")
        assert stop == Decimal("95")

    def test_long_price_rises_moves_stop_up(self) -> None:
        hw, stop = compute_trailing_stop(
            OrderSide.BUY, Decimal("110"), Decimal("5"), Decimal("100")
        )
        assert hw == Decimal("110")
        assert stop == Decimal("105")

    def test_long_price_falls_does_not_move_stop(self) -> None:
        hw, stop = compute_trailing_stop(OrderSide.BUY, Decimal("95"), Decimal("5"), Decimal("110"))
        assert hw == Decimal("110")  # high-water no baja
        assert stop == Decimal("105")

    def test_short_first_tick_sets_low_water(self) -> None:
        hw, stop = compute_trailing_stop(OrderSide.SELL, Decimal("100"), Decimal("5"), None)
        assert hw == Decimal("100")
        assert stop == Decimal("105")

    def test_short_price_falls_moves_stop_down(self) -> None:
        hw, stop = compute_trailing_stop(
            OrderSide.SELL, Decimal("90"), Decimal("5"), Decimal("100")
        )
        assert hw == Decimal("90")
        assert stop == Decimal("95")

    def test_short_price_rises_does_not_move_stop(self) -> None:
        hw, stop = compute_trailing_stop(
            OrderSide.SELL, Decimal("105"), Decimal("5"), Decimal("90")
        )
        assert hw == Decimal("90")  # low-water no sube
        assert stop == Decimal("95")


class TestIsTrailingStopHit:
    def test_long_hit(self) -> None:
        assert is_trailing_stop_hit(OrderSide.BUY, Decimal("94"), Decimal("95")) is True

    def test_long_exact(self) -> None:
        assert is_trailing_stop_hit(OrderSide.BUY, Decimal("95"), Decimal("95")) is True

    def test_long_not_hit(self) -> None:
        assert is_trailing_stop_hit(OrderSide.BUY, Decimal("96"), Decimal("95")) is False

    def test_short_hit(self) -> None:
        assert is_trailing_stop_hit(OrderSide.SELL, Decimal("96"), Decimal("95")) is True

    def test_short_not_hit(self) -> None:
        assert is_trailing_stop_hit(OrderSide.SELL, Decimal("94"), Decimal("95")) is False


# ---------------------------------------------------------------------------
# update_high_water / trailing_stop_from_delta (funciones puras)
# ---------------------------------------------------------------------------


class TestUpdateHighWater:
    def test_long_first_tick(self) -> None:
        assert update_high_water(OrderSide.BUY, Decimal("100"), None) == Decimal("100")

    def test_long_rises(self) -> None:
        assert update_high_water(OrderSide.BUY, Decimal("110"), Decimal("100")) == Decimal("110")

    def test_long_falls_keeps_high(self) -> None:
        assert update_high_water(OrderSide.BUY, Decimal("90"), Decimal("100")) == Decimal("100")

    def test_short_first_tick(self) -> None:
        assert update_high_water(OrderSide.SELL, Decimal("100"), None) == Decimal("100")

    def test_short_falls(self) -> None:
        assert update_high_water(OrderSide.SELL, Decimal("90"), Decimal("100")) == Decimal("90")

    def test_short_rises_keeps_low(self) -> None:
        assert update_high_water(OrderSide.SELL, Decimal("110"), Decimal("100")) == Decimal("100")


class TestTrailingStopFromDelta:
    def test_long_subtracts(self) -> None:
        stop = trailing_stop_from_delta(OrderSide.BUY, Decimal("100"), Decimal("5"))
        assert stop == Decimal("95")

    def test_short_adds(self) -> None:
        stop = trailing_stop_from_delta(OrderSide.SELL, Decimal("100"), Decimal("5"))
        assert stop == Decimal("105")


# ---------------------------------------------------------------------------
# resolve_trailing_delta (función pura) — modos FIXED / PERCENT / ATR
# ---------------------------------------------------------------------------


class TestResolveTrailingDelta:
    def test_fixed_returns_delta(self) -> None:
        delta = resolve_trailing_delta(
            TrailingMode.FIXED, Decimal("50000"), fixed_delta=Decimal("1000")
        )
        assert delta == Decimal("1000")

    def test_fixed_missing_raises(self) -> None:
        with pytest.raises(ValueError, match="FIXED trailing requires fixed_delta"):
            resolve_trailing_delta(TrailingMode.FIXED, Decimal("50000"))

    def test_percent_of_reference(self) -> None:
        delta = resolve_trailing_delta(
            TrailingMode.PERCENT, Decimal("50000"), percent=Decimal("0.02")
        )
        assert delta == Decimal("1000.00")

    def test_percent_missing_raises(self) -> None:
        with pytest.raises(ValueError, match="PERCENT trailing requires percent"):
            resolve_trailing_delta(TrailingMode.PERCENT, Decimal("50000"))

    def test_atr_times_multiplier(self) -> None:
        delta = resolve_trailing_delta(
            TrailingMode.ATR,
            Decimal("50000"),
            atr_value=Decimal("400"),
            atr_multiplier=Decimal("2.5"),
        )
        assert delta == Decimal("1000.0")

    def test_atr_default_multiplier_is_one(self) -> None:
        delta = resolve_trailing_delta(TrailingMode.ATR, Decimal("50000"), atr_value=Decimal("400"))
        assert delta == Decimal("400")

    def test_atr_missing_raises(self) -> None:
        with pytest.raises(ValueError, match="ATR trailing requires atr_value"):
            resolve_trailing_delta(TrailingMode.ATR, Decimal("50000"))


# ---------------------------------------------------------------------------
# maybe_move_to_break_even (función pura)
# ---------------------------------------------------------------------------


class TestMaybeMoveToBE:
    def test_long_trigger_not_reached(self) -> None:
        result = maybe_move_to_break_even(
            OrderSide.BUY, Decimal("100"), Decimal("104"), Decimal("10"), None
        )
        assert result is None

    def test_long_trigger_reached_no_sl(self) -> None:
        result = maybe_move_to_break_even(
            OrderSide.BUY, Decimal("100"), Decimal("115"), Decimal("10"), None
        )
        assert result == Decimal("100")

    def test_long_sl_already_above_entry(self) -> None:
        result = maybe_move_to_break_even(
            OrderSide.BUY, Decimal("100"), Decimal("115"), Decimal("10"), Decimal("102")
        )
        assert result is None

    def test_short_trigger_reached(self) -> None:
        result = maybe_move_to_break_even(
            OrderSide.SELL, Decimal("100"), Decimal("85"), Decimal("10"), None
        )
        assert result == Decimal("100")

    def test_short_sl_already_below_entry(self) -> None:
        result = maybe_move_to_break_even(
            OrderSide.SELL, Decimal("100"), Decimal("85"), Decimal("10"), Decimal("98")
        )
        assert result is None


# ---------------------------------------------------------------------------
# PositionManager — SL estático
# ---------------------------------------------------------------------------


class TestPositionManagerSL:
    def test_long_sl_hit(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))

        result = pm.tick("BTCUSDT", Decimal("47999"))

        assert result.trigger == PositionTriggerReason.SL_HIT
        assert result.close_order_id is not None
        # La posición debe haberse cerrado
        assert adapter.get_position("BTCUSDT") is None

    def test_long_sl_exact_price(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))

        result = pm.tick("BTCUSDT", Decimal("48000"))
        assert result.trigger == PositionTriggerReason.SL_HIT

    def test_long_sl_not_hit(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))

        result = pm.tick("BTCUSDT", Decimal("49000"))
        assert result.trigger == PositionTriggerReason.NONE
        assert result.close_order_id is None

    def test_short_sl_hit(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("52000")))

        result = pm.tick("BTCUSDT", Decimal("52001"))
        assert result.trigger == PositionTriggerReason.SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_short_sl_not_hit(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("52000")))

        result = pm.tick("BTCUSDT", Decimal("51000"))
        assert result.trigger == PositionTriggerReason.NONE


# ---------------------------------------------------------------------------
# PositionManager — TP
# ---------------------------------------------------------------------------


class TestPositionManagerTP:
    def test_long_tp_hit(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", take_profit=Decimal("55000")))

        result = pm.tick("BTCUSDT", Decimal("55001"))
        assert result.trigger == PositionTriggerReason.TP_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_long_tp_exact(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", take_profit=Decimal("55000")))

        result = pm.tick("BTCUSDT", Decimal("55000"))
        assert result.trigger == PositionTriggerReason.TP_HIT

    def test_short_tp_hit(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", take_profit=Decimal("45000")))

        result = pm.tick("BTCUSDT", Decimal("44999"))
        assert result.trigger == PositionTriggerReason.TP_HIT
        assert adapter.get_position("BTCUSDT") is None


# ---------------------------------------------------------------------------
# PositionManager — trailing stop
# ---------------------------------------------------------------------------


class TestPositionManagerTrailing:
    def test_long_trailing_follows_price_up(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_delta=Decimal("1000")))

        # Precio sube: trailing stop se mueve a 52000
        r = pm.tick("BTCUSDT", Decimal("53000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52000")

        # Precio baja pero no toca el trailing → NONE
        r = pm.tick("BTCUSDT", Decimal("52500"))
        assert r.trigger == PositionTriggerReason.NONE

        # Precio cae al trailing stop → cierre
        r = pm.tick("BTCUSDT", Decimal("52000"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_short_trailing_follows_price_down(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_delta=Decimal("1000")))

        r = pm.tick("BTCUSDT", Decimal("47000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("48000")

        r = pm.tick("BTCUSDT", Decimal("48000"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_trailing_not_hit_on_first_tick_at_reasonable_price(self) -> None:
        """El trailing stop no se dispara en el primer tick si el precio es mayor que delta.

        En el primer tick high_water = mark_price, trailing_stop = mark_price - delta.
        Como mark_price > trailing_stop (delta > 0), nunca se activa en ese mismo tick.
        """
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_delta=Decimal("1000")))

        r = pm.tick("BTCUSDT", Decimal("50000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("49000")

    def test_trailing_high_water_resets_on_reconfigure(self) -> None:
        """Al llamar set_config de nuevo, el high-water se resetea."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_delta=Decimal("1000")))

        # Primer tick: trailing stop establecido
        pm.tick("BTCUSDT", Decimal("55000"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("54000")

        # Reconfigurar → trailing stop se resetea
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_delta=Decimal("500")))
        assert pm.get_trailing_stop("BTCUSDT") is None


# ---------------------------------------------------------------------------
# PositionManager — trailing stop por PORCENTAJE y ATR (F14), series sintéticas
# ---------------------------------------------------------------------------


class TestPositionManagerTrailingPercent:
    def test_long_percent_stop_grows_with_price(self) -> None:
        """PERCENT LONG: la distancia se recalcula sobre el high-water (crece con el precio)."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_percent=Decimal("0.02")))

        # Sube a 53000: stop = 53000 - 53000*0.02 = 51940
        r = pm.tick("BTCUSDT", Decimal("53000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("51940.00")

        # Sube a 55000: stop = 55000 - 1100 = 53900 (creció con el high-water)
        r = pm.tick("BTCUSDT", Decimal("55000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("53900.00")

        # Cae al stop (high_water sigue en 55000) → cierre
        r = pm.tick("BTCUSDT", Decimal("53900"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_short_percent_stop_follows_price_down(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_percent=Decimal("0.02")))

        # Baja a 45000: stop = 45000 + 45000*0.02 = 45900
        r = pm.tick("BTCUSDT", Decimal("45000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("45900.00")

        # Rebota al stop → cierre
        r = pm.tick("BTCUSDT", Decimal("45900"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None


class TestPositionManagerTrailingATR:
    def test_long_atr_constant_distance(self) -> None:
        """ATR LONG: distancia = atr_value * multiplier (constante). 400 * 2.5 = 1000."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                trailing_atr=Decimal("400"),
                trailing_atr_multiplier=Decimal("2.5"),
            )
        )

        r = pm.tick("BTCUSDT", Decimal("53000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52000.0")

        # Retroceso que no toca el stop
        r = pm.tick("BTCUSDT", Decimal("52500"))
        assert r.trigger == PositionTriggerReason.NONE

        # Cae al stop → cierre
        r = pm.tick("BTCUSDT", Decimal("52000"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_short_atr_constant_distance(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                trailing_atr=Decimal("400"),
                trailing_atr_multiplier=Decimal("2.5"),
            )
        )

        r = pm.tick("BTCUSDT", Decimal("47000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("48000.0")

        r = pm.tick("BTCUSDT", Decimal("48000"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_atr_default_multiplier(self) -> None:
        """Sin multiplier, la distancia ATR es el atr_value crudo (multiplier=1)."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_atr=Decimal("1000")))

        r = pm.tick("BTCUSDT", Decimal("53000"))
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52000")


# ---------------------------------------------------------------------------
# PositionManager — casos edge
# ---------------------------------------------------------------------------


class TestPositionManagerEdge:
    def test_no_position_returns_none(self) -> None:
        adapter = PaperAdapter()
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))

        result = pm.tick("BTCUSDT", Decimal("47000"))
        assert result.trigger == PositionTriggerReason.NONE

    def test_no_config_returns_none(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)

        result = pm.tick("BTCUSDT", Decimal("47000"))
        assert result.trigger == PositionTriggerReason.NONE

    def test_config_removed_after_trigger(self) -> None:
        """Después de un trigger, la config se limpia: un tick posterior retorna NONE."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))

        r1 = pm.tick("BTCUSDT", Decimal("47000"))
        assert r1.trigger == PositionTriggerReason.SL_HIT

        # Segundo tick: sin posición y sin config
        r2 = pm.tick("BTCUSDT", Decimal("46000"))
        assert r2.trigger == PositionTriggerReason.NONE
        assert r2.close_order_id is None

    def test_sl_priority_over_tp(self) -> None:
        """SL tiene prioridad sobre TP (evaluación en orden: SL → TP → trailing)."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        # Caso imposible en producción pero verifica el orden de evaluación
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit=Decimal("48000"),
            )
        )

        result = pm.tick("BTCUSDT", Decimal("48000"))
        assert result.trigger == PositionTriggerReason.SL_HIT

    def test_multiple_symbols_independent(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        _open_long(adapter, "ETHUSDT", Decimal("1"), Decimal("3000"))

        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))
        pm.set_config(PositionConfig(symbol="ETHUSDT", stop_loss=Decimal("2800")))

        r_btc = pm.tick("BTCUSDT", Decimal("47000"))
        r_eth = pm.tick("ETHUSDT", Decimal("3100"))

        assert r_btc.trigger == PositionTriggerReason.SL_HIT
        assert r_eth.trigger == PositionTriggerReason.NONE
        assert adapter.get_position("BTCUSDT") is None
        assert adapter.get_position("ETHUSDT") is not None


# ---------------------------------------------------------------------------
# PositionConfig — model_validator
# ---------------------------------------------------------------------------


class TestPositionConfigValidator:
    def test_all_none_raises(self) -> None:

        with pytest.raises(ValueError, match="At least one"):
            PositionConfig(symbol="BTCUSDT")

    def test_only_sl_valid(self) -> None:
        cfg = PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000"))
        assert cfg.stop_loss == Decimal("48000")

    def test_only_tp_valid(self) -> None:
        cfg = PositionConfig(symbol="BTCUSDT", take_profit=Decimal("55000"))
        assert cfg.take_profit == Decimal("55000")

    def test_only_trailing_valid(self) -> None:
        cfg = PositionConfig(symbol="BTCUSDT", trailing_delta=Decimal("1000"))
        assert cfg.trailing_delta == Decimal("1000")
        assert cfg.resolved_trailing_mode == TrailingMode.FIXED

    def test_only_trailing_percent_valid(self) -> None:
        cfg = PositionConfig(symbol="BTCUSDT", trailing_percent=Decimal("0.02"))
        assert cfg.trailing_percent == Decimal("0.02")
        assert cfg.resolved_trailing_mode == TrailingMode.PERCENT

    def test_only_trailing_atr_valid(self) -> None:
        cfg = PositionConfig(symbol="BTCUSDT", trailing_atr=Decimal("400"))
        assert cfg.trailing_atr == Decimal("400")
        assert cfg.resolved_trailing_mode == TrailingMode.ATR

    def test_no_trailing_mode_is_none(self) -> None:
        cfg = PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000"))
        assert cfg.resolved_trailing_mode is None

    def test_trailing_modes_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            PositionConfig(
                symbol="BTCUSDT",
                trailing_delta=Decimal("1000"),
                trailing_percent=Decimal("0.02"),
            )

    def test_atr_multiplier_requires_atr(self) -> None:
        with pytest.raises(ValueError, match="trailing_atr_multiplier requires trailing_atr"):
            PositionConfig(
                symbol="BTCUSDT",
                trailing_delta=Decimal("1000"),
                trailing_atr_multiplier=Decimal("2.5"),
            )

    def test_be_sl_offset_ge_trigger_delta_raises(self) -> None:
        with pytest.raises(ValueError, match="be_sl_offset must be less than be_trigger_delta"):
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                be_trigger_delta=Decimal("500"),
                be_sl_offset=Decimal("500"),
            )

    def test_be_sl_offset_gt_trigger_delta_raises(self) -> None:
        with pytest.raises(ValueError, match="be_sl_offset must be less than be_trigger_delta"):
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                be_trigger_delta=Decimal("500"),
                be_sl_offset=Decimal("600"),
            )

    def test_be_sl_offset_without_trigger_delta_raises(self) -> None:
        with pytest.raises(ValueError, match="be_sl_offset has no effect"):
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                be_sl_offset=Decimal("50"),
            )


# ---------------------------------------------------------------------------
# PositionManager — break-even integrado
# ---------------------------------------------------------------------------


class TestPositionManagerBreakEven:
    def test_long_be_moves_sl_to_entry(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        # entry_price real = 50025 (slippage), SL inicial = 48000, be_trigger_delta = 3000
        entry_price = adapter.get_position("BTCUSDT").entry_price  # ~50025
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                be_trigger_delta=Decimal("3000"),
            )
        )

        # Tick en el que precio sube be_trigger_delta a favor → SL se mueve a entry
        trigger_price = entry_price + Decimal("3000")
        r = pm.tick("BTCUSDT", trigger_price)
        assert r.trigger == PositionTriggerReason.NONE
        assert pm.get_effective_sl("BTCUSDT") == entry_price

    def test_long_be_sl_then_triggers(self) -> None:
        """Después de mover SL a break-even, si el precio cae por debajo → SL_HIT."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        entry_price = adapter.get_position("BTCUSDT").entry_price
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                be_trigger_delta=Decimal("3000"),
            )
        )

        # Activar break-even
        pm.tick("BTCUSDT", entry_price + Decimal("3000"))
        assert pm.get_effective_sl("BTCUSDT") == entry_price

        # Precio cae por debajo del entry → SL_HIT con SL break-even
        r = pm.tick("BTCUSDT", entry_price - Decimal("1"))
        assert r.trigger == PositionTriggerReason.SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_be_not_triggered_below_delta(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        entry_price = adapter.get_position("BTCUSDT").entry_price
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                be_trigger_delta=Decimal("3000"),
            )
        )

        # Precio sube pero no llega al delta → SL efectivo sigue en 48000
        pm.tick("BTCUSDT", entry_price + Decimal("1000"))
        assert pm.get_effective_sl("BTCUSDT") == Decimal("48000")

    def test_short_be_moves_sl_to_entry(self) -> None:
        """SHORT: be_trigger_delta mueve SL a entry_price cuando precio cae lo suficiente."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        entry_price = adapter.get_position("BTCUSDT").entry_price
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("52000"),
                be_trigger_delta=Decimal("3000"),
            )
        )

        # Precio cae be_trigger_delta a favor → SL se mueve a entry_price
        pm.tick("BTCUSDT", entry_price - Decimal("3000"))
        assert pm.get_effective_sl("BTCUSDT") == entry_price

        # Precio sube por encima del entry → SL_HIT con SL break-even
        r = pm.tick("BTCUSDT", entry_price + Decimal("1"))
        assert r.trigger == PositionTriggerReason.SL_HIT

    def test_be_without_initial_sl_sets_entry_as_sl(self) -> None:
        """be_trigger_delta sin stop_loss inicial igual setea entry_price como SL efectivo."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        entry_price = adapter.get_position("BTCUSDT").entry_price
        pm = PositionManager(adapter)
        # Sin stop_loss, solo take_profit + be_trigger_delta
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                take_profit=Decimal("60000"),
                be_trigger_delta=Decimal("2000"),
            )
        )
        assert pm.get_effective_sl("BTCUSDT") is None

        # Activar break-even → SL efectivo = entry_price
        pm.tick("BTCUSDT", entry_price + Decimal("2000"))
        assert pm.get_effective_sl("BTCUSDT") == entry_price

        # Si el precio cae por debajo del entry → SL_HIT
        r = pm.tick("BTCUSDT", entry_price - Decimal("1"))
        assert r.trigger == PositionTriggerReason.SL_HIT


# ---------------------------------------------------------------------------
# maybe_move_to_break_even — be_sl_offset (función pura)
# ---------------------------------------------------------------------------


class TestMaybeMoveToBreakEvenOffset:
    def test_long_offset_moves_sl_above_entry(self) -> None:
        result = maybe_move_to_break_even(
            side=OrderSide.BUY,
            entry_price=Decimal("50000"),
            mark_price=Decimal("53000"),
            be_trigger_delta=Decimal("3000"),
            current_sl=Decimal("48000"),
            be_sl_offset=Decimal("100"),
        )
        assert result == Decimal("50100")

    def test_short_offset_moves_sl_below_entry(self) -> None:
        result = maybe_move_to_break_even(
            side=OrderSide.SELL,
            entry_price=Decimal("50000"),
            mark_price=Decimal("47000"),
            be_trigger_delta=Decimal("3000"),
            current_sl=Decimal("52000"),
            be_sl_offset=Decimal("100"),
        )
        assert result == Decimal("49900")

    def test_long_offset_no_move_when_sl_already_at_target(self) -> None:
        result = maybe_move_to_break_even(
            side=OrderSide.BUY,
            entry_price=Decimal("50000"),
            mark_price=Decimal("53000"),
            be_trigger_delta=Decimal("3000"),
            current_sl=Decimal("50100"),
            be_sl_offset=Decimal("100"),
        )
        assert result is None

    def test_long_offset_zero_default_returns_entry(self) -> None:
        result = maybe_move_to_break_even(
            side=OrderSide.BUY,
            entry_price=Decimal("50000"),
            mark_price=Decimal("53000"),
            be_trigger_delta=Decimal("3000"),
            current_sl=Decimal("48000"),
        )
        assert result == Decimal("50000")


class TestPositionManagerBreakEvenOffset:
    def test_long_be_sl_offset_moves_sl_above_entry(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        entry_price = adapter.get_position("BTCUSDT").entry_price
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                be_trigger_delta=Decimal("3000"),
                be_sl_offset=Decimal("50"),
            )
        )

        pm.tick("BTCUSDT", entry_price + Decimal("3000"))
        assert pm.get_effective_sl("BTCUSDT") == entry_price + Decimal("50")

    def test_short_be_sl_offset_moves_sl_below_entry(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        entry_price = adapter.get_position("BTCUSDT").entry_price
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("52000"),
                be_trigger_delta=Decimal("3000"),
                be_sl_offset=Decimal("50"),
            )
        )

        pm.tick("BTCUSDT", entry_price - Decimal("3000"))
        assert pm.get_effective_sl("BTCUSDT") == entry_price - Decimal("50")

    def test_be_offset_does_not_fire_twice(self) -> None:
        """Un segundo tick con precio aún favorable no mueve el SL más lejos."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        entry_price = adapter.get_position("BTCUSDT").entry_price
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                be_trigger_delta=Decimal("3000"),
                be_sl_offset=Decimal("50"),
            )
        )

        pm.tick("BTCUSDT", entry_price + Decimal("3000"))
        expected_sl = entry_price + Decimal("50")
        assert pm.get_effective_sl("BTCUSDT") == expected_sl

        pm.tick("BTCUSDT", entry_price + Decimal("4000"))
        assert pm.get_effective_sl("BTCUSDT") == expected_sl


# ---------------------------------------------------------------------------
# F14 — Multi-TP (take_profit_levels)
# ---------------------------------------------------------------------------


class TestMultiTP:
    def test_long_two_levels_first_partial(self) -> None:
        """Primer nivel de multi-TP cierra parcialmente; posición sigue abierta."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("0.5")),
                    TakeProfitLevel(price=Decimal("60000"), close_fraction=Decimal("0.5")),
                ],
            )
        )

        r = pm.tick("BTCUSDT", Decimal("55000"))

        assert r.trigger == PositionTriggerReason.TP_PARTIAL
        assert r.tp_level_index == 0
        assert r.closed_fraction == Decimal("0.5")
        assert r.close_order_id is not None
        # Posición sigue abierta con ~50% de la cantidad
        pos = adapter.get_position("BTCUSDT")
        assert pos is not None

    def test_long_two_levels_second_closes_all(self) -> None:
        """Segundo nivel cierra el resto; trigger es TP_HIT (cierre total).

        close_fraction es fracción del remanente en cada nivel: para cerrar todo en
        dos niveles, el segundo nivel debe tener close_fraction=1.
        """
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("0.5")),
                    TakeProfitLevel(price=Decimal("60000"), close_fraction=Decimal("1")),
                ],
            )
        )

        pm.tick("BTCUSDT", Decimal("55000"))  # TP1 → TP_PARTIAL
        r = pm.tick("BTCUSDT", Decimal("60000"))  # TP2 → TP_HIT

        assert r.trigger == PositionTriggerReason.TP_HIT
        assert r.tp_level_index == 1
        assert r.closed_fraction == Decimal("1")
        assert adapter.get_position("BTCUSDT") is None

    def test_sl_still_triggers_after_partial_tp(self) -> None:
        """SL sigue activo después de un cierre parcial por TP."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("0.5")),
                    TakeProfitLevel(price=Decimal("60000"), close_fraction=Decimal("0.5")),
                ],
            )
        )

        pm.tick("BTCUSDT", Decimal("55000"))  # TP1 parcial
        r = pm.tick("BTCUSDT", Decimal("47999"))  # SL hit

        assert r.trigger == PositionTriggerReason.SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_short_multi_tp(self) -> None:
        """Multi-TP en SHORT: niveles descendentes. El último nivel usa close_fraction=1."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("52000"),
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("47000"), close_fraction=Decimal("0.5")),
                    TakeProfitLevel(price=Decimal("44000"), close_fraction=Decimal("1")),
                ],
            )
        )

        r1 = pm.tick("BTCUSDT", Decimal("47000"))
        assert r1.trigger == PositionTriggerReason.TP_PARTIAL
        assert adapter.get_position("BTCUSDT") is not None

        r2 = pm.tick("BTCUSDT", Decimal("44000"))
        assert r2.trigger == PositionTriggerReason.TP_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_tp_levels_mutual_exclusion_with_take_profit(self) -> None:
        """take_profit y take_profit_levels no pueden coexistir."""

        with pytest.raises(ValueError, match="mutually exclusive"):
            PositionConfig(
                symbol="BTCUSDT",
                take_profit=Decimal("55000"),
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("1"))
                ],
            )

    def test_tp_levels_each_fraction_at_most_one(self) -> None:
        """Cada nivel individual debe tener close_fraction <= 1."""

        with pytest.raises(ValueError):
            TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("1.1"))

    def test_trailing_stop_continues_after_partial_tp(self) -> None:
        """El trailing stop sigue avanzando y disparándose tras un cierre parcial de TP."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                trailing_delta=Decimal("1000"),
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("0.5")),
                    TakeProfitLevel(price=Decimal("60000"), close_fraction=Decimal("1")),
                ],
            )
        )

        # TP1 parcial
        pm.tick("BTCUSDT", Decimal("55000"))
        assert adapter.get_position("BTCUSDT") is not None

        # Precio sube: trailing stop debe avanzar
        pm.tick("BTCUSDT", Decimal("57000"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("56000")

        # Precio cae al trailing → cierra el remanente
        r = pm.tick("BTCUSDT", Decimal("56000"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_remaining_levels_accessible(self) -> None:
        """get_remaining_tp_levels refleja niveles pendientes."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("0.5")),
                    TakeProfitLevel(price=Decimal("60000"), close_fraction=Decimal("0.5")),
                ],
            )
        )

        assert len(pm.get_remaining_tp_levels("BTCUSDT")) == 2
        pm.tick("BTCUSDT", Decimal("55000"))
        assert len(pm.get_remaining_tp_levels("BTCUSDT")) == 1

    def test_partial_tp_level_preserved_when_order_fails(self) -> None:
        """Si place_order falla en un nivel parcial, el nivel no se consume (reintento)."""

        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("0.5")),
                    TakeProfitLevel(price=Decimal("60000"), close_fraction=Decimal("1")),
                ],
            )
        )

        with patch.object(pm, "_place_close_order", side_effect=RuntimeError("order failed")):
            with pytest.raises(RuntimeError, match="order failed"):
                pm.tick("BTCUSDT", Decimal("55000"))

        # Nivel 0 debe seguir pendiente para que el próximo tick reintente
        assert len(pm.get_remaining_tp_levels("BTCUSDT")) == 2
        # Config sigue activa — posición bajo monitoreo
        assert pm.get_config("BTCUSDT") is not None

    def test_last_tp_level_config_removed_even_when_order_fails(self) -> None:
        """Si place_order falla en el último nivel (full-close), la config se elimina igual."""

        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("60000"), close_fraction=Decimal("1")),
                ],
            )
        )

        with patch.object(pm, "_place_close_order", side_effect=RuntimeError("order failed")):
            with pytest.raises(RuntimeError, match="order failed"):
                pm.tick("BTCUSDT", Decimal("60000"))

        # Config eliminada (evita doble orden en el siguiente tick)
        assert pm.get_config("BTCUSDT") is None


# ---------------------------------------------------------------------------
# F14 — Actualización dinámica de SL/TP
# ---------------------------------------------------------------------------


class TestDynamicSlTpUpdate:
    def test_update_sl_changes_effective_sl(self) -> None:
        """update_sl actualiza el SL efectivo sin resetear trailing."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                trailing_delta=Decimal("1000"),
            )
        )

        # Avanzar trailing stop
        pm.tick("BTCUSDT", Decimal("53000"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52000")

        # Actualizar SL dinámicamente — trailing no se resetea
        pm.update_sl("BTCUSDT", Decimal("51000"))
        assert pm.get_effective_sl("BTCUSDT") == Decimal("51000")
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52000")

    def test_update_sl_triggers_on_next_tick(self) -> None:
        """El nuevo SL se evalúa en el siguiente tick."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("45000")))

        pm.update_sl("BTCUSDT", Decimal("49000"))
        r = pm.tick("BTCUSDT", Decimal("48999"))

        assert r.trigger == PositionTriggerReason.SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_update_tp_changes_effective_tp(self) -> None:
        """update_tp actualiza el TP dinámicamente."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", take_profit=Decimal("55000")))

        # Precio no llega al TP original
        r = pm.tick("BTCUSDT", Decimal("52000"))
        assert r.trigger == PositionTriggerReason.NONE

        # Bajar el TP
        pm.update_tp("BTCUSDT", Decimal("52000"))
        r = pm.tick("BTCUSDT", Decimal("52000"))
        assert r.trigger == PositionTriggerReason.TP_HIT

    def test_update_tp_clears_multi_tp_levels(self) -> None:
        """update_tp convierte multi-TP a single TP."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                take_profit_levels=[
                    TakeProfitLevel(price=Decimal("55000"), close_fraction=Decimal("0.5")),
                    TakeProfitLevel(price=Decimal("60000"), close_fraction=Decimal("0.5")),
                ],
            )
        )

        pm.update_tp("BTCUSDT", Decimal("53000"))
        assert pm.get_remaining_tp_levels("BTCUSDT") == []
        assert pm.get_effective_tp("BTCUSDT") == Decimal("53000")

        r = pm.tick("BTCUSDT", Decimal("53000"))
        assert r.trigger == PositionTriggerReason.TP_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_update_sl_on_missing_symbol_raises(self) -> None:
        """update_sl sobre símbolo sin config lanza KeyError."""

        adapter = PaperAdapter()
        pm = PositionManager(adapter)
        with pytest.raises(KeyError):
            pm.update_sl("BTCUSDT", Decimal("49000"))

    def test_update_tp_on_missing_symbol_raises(self) -> None:
        """update_tp sobre símbolo sin config lanza KeyError."""

        adapter = PaperAdapter()
        pm = PositionManager(adapter)
        with pytest.raises(KeyError):
            pm.update_tp("BTCUSDT", Decimal("55000"))


# ---------------------------------------------------------------------------
# F14 — Invalidación de setup
# ---------------------------------------------------------------------------


class TestSetupInvalidation:
    def test_invalidation_moves_sl(self) -> None:
        """trigger_setup_invalidation mueve el SL efectivo al valor configurado."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                invalidation_action=InvalidationAction(new_sl=Decimal("49500")),
            )
        )

        result = pm.trigger_setup_invalidation("BTCUSDT", Decimal("51000"))

        assert result is not None
        assert result.trigger == PositionTriggerReason.SETUP_INVALIDATED
        assert result.close_order_id is None
        assert pm.get_effective_sl("BTCUSDT") == Decimal("49500")
        # Posición sigue abierta
        assert adapter.get_position("BTCUSDT") is not None

    def test_invalidation_partial_close(self) -> None:
        """trigger_setup_invalidation cierra una fracción de la posición."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        initial_qty = adapter.get_position("BTCUSDT").quantity
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                invalidation_action=InvalidationAction(close_fraction=Decimal("0.5")),
            )
        )

        result = pm.trigger_setup_invalidation("BTCUSDT", Decimal("51000"))

        assert result is not None
        assert result.trigger == PositionTriggerReason.SETUP_INVALIDATED
        assert result.close_order_id is not None
        assert result.closed_fraction == Decimal("0.5")
        # Posición sigue abierta con el 50% restante
        pos = adapter.get_position("BTCUSDT")
        assert pos is not None
        assert pos.quantity < initial_qty
        # Config sigue activa (cierre parcial no la elimina)
        assert pm.get_config("BTCUSDT") is not None

    def test_invalidation_full_close_removes_config(self) -> None:
        """Invalidación con close_fraction=1 cierra la posición y elimina config."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                invalidation_action=InvalidationAction(close_fraction=Decimal("1")),
            )
        )

        result = pm.trigger_setup_invalidation("BTCUSDT", Decimal("51000"))

        assert result is not None
        assert result.trigger == PositionTriggerReason.SETUP_INVALIDATED
        assert adapter.get_position("BTCUSDT") is None
        assert pm.get_config("BTCUSDT") is None

    def test_invalidation_sl_and_partial_close_combined(self) -> None:
        """Invalidación combina mover SL y cierre parcial en un solo call."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                invalidation_action=InvalidationAction(
                    new_sl=Decimal("49800"),
                    close_fraction=Decimal("0.5"),
                ),
            )
        )

        result = pm.trigger_setup_invalidation("BTCUSDT", Decimal("51000"))

        assert result is not None
        assert pm.get_effective_sl("BTCUSDT") == Decimal("49800")
        assert result.close_order_id is not None
        assert adapter.get_position("BTCUSDT") is not None

    def test_invalidation_no_action_returns_none(self) -> None:
        """Sin invalidation_action configurada retorna None."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))

        result = pm.trigger_setup_invalidation("BTCUSDT", Decimal("51000"))

        assert result is None

    def test_invalidation_no_position_returns_none(self) -> None:
        """Sin posición abierta retorna None."""
        adapter = PaperAdapter()
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                invalidation_action=InvalidationAction(new_sl=Decimal("49000")),
            )
        )

        result = pm.trigger_setup_invalidation("BTCUSDT", Decimal("50000"))

        assert result is None

    def test_invalidation_action_requires_at_least_one_field(self) -> None:
        """InvalidationAction sin new_sl ni close_fraction > 0 falla."""

        with pytest.raises(ValueError, match="at least"):
            InvalidationAction()

    def test_new_sl_after_invalidation_used_in_next_tick(self) -> None:
        """Tras invalidación con new_sl, el nuevo SL se evalúa en el siguiente tick."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("45000"),
                invalidation_action=InvalidationAction(new_sl=Decimal("49500")),
            )
        )

        pm.trigger_setup_invalidation("BTCUSDT", Decimal("51000"))
        assert pm.get_effective_sl("BTCUSDT") == Decimal("49500")

        # Precio cae por debajo del nuevo SL → SL_HIT
        r = pm.tick("BTCUSDT", Decimal("49499"))
        assert r.trigger == PositionTriggerReason.SL_HIT


# ---------------------------------------------------------------------------
# F14 — Trailing ATR dinámico (feed en vivo por tick)
# ---------------------------------------------------------------------------


class TestTrailingAtrDynamic:
    def test_long_atr_widens_stop_as_volatility_grows(self) -> None:
        """Serie sintética LONG: ATR creciente ensancha la distancia del trailing stop."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                trailing_atr_dynamic=True,
                trailing_atr_multiplier=Decimal("2"),
            )
        )

        # tick 1: price=53000, atr=500 → delta=1000 → stop=52000
        pm.tick("BTCUSDT", Decimal("53000"), atr=Decimal("500"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52000")

        # tick 2: price=55000, atr=800 → delta=1600 → stop=53400
        pm.tick("BTCUSDT", Decimal("55000"), atr=Decimal("800"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("53400")

        # tick 3: price cae al stop → TRAILING_SL_HIT
        r = pm.tick("BTCUSDT", Decimal("53400"), atr=Decimal("800"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_long_atr_narrows_stop_as_volatility_decreases(self) -> None:
        """Serie sintética LONG: ATR decreciente estrecha la distancia del stop."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_atr_dynamic=True))

        # ATR grande: stop lejos (53000 - 2000 = 51000)
        pm.tick("BTCUSDT", Decimal("53000"), atr=Decimal("2000"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("51000")

        # ATR más chico: stop más cerca (54000 - 500 = 53500)
        pm.tick("BTCUSDT", Decimal("54000"), atr=Decimal("500"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("53500")

    def test_short_atr_dynamic(self) -> None:
        """Serie sintética SHORT: ATR dinámico ajusta el stop en la dirección correcta."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_short(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                trailing_atr_dynamic=True,
                trailing_atr_multiplier=Decimal("2"),
            )
        )

        # tick 1: price=47000, atr=500 → delta=1000 → stop=48000
        pm.tick("BTCUSDT", Decimal("47000"), atr=Decimal("500"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("48000")

        # tick 2: price=45000, atr=800 → delta=1600 → stop=46600
        pm.tick("BTCUSDT", Decimal("45000"), atr=Decimal("800"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("46600")

        # tick 3: price sube al stop → cierre
        r = pm.tick("BTCUSDT", Decimal("46600"), atr=Decimal("800"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_smoothing_dampens_atr_spike(self) -> None:
        """EMA smoothing: un pico puntual de ATR no desplaza el stop de forma abrupta.

        Sin suavizado (alpha=1): spike de 500→5000 mueve el stop 4500 puntos extra.
        Con alpha=0.1: ATR suavizado ≈ 950 → impacto reducido ~10x.
        """
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                trailing_atr_dynamic=True,
                trailing_atr_smoothing_alpha=Decimal("0.1"),
            )
        )

        # ATR estable en 500 durante 3 ticks → stop = high_water - 500
        pm.tick("BTCUSDT", Decimal("51000"), atr=Decimal("500"))
        pm.tick("BTCUSDT", Decimal("52000"), atr=Decimal("500"))
        pm.tick("BTCUSDT", Decimal("53000"), atr=Decimal("500"))
        stop_before_spike = pm.get_trailing_stop("BTCUSDT")  # 53000 - 500 = 52500

        # Spike: ATR salta a 5000 (10x). EMA suavizado: 0.1*5000 + 0.9*500 = 950.
        pm.tick("BTCUSDT", Decimal("54000"), atr=Decimal("5000"))
        stop_after_spike = pm.get_trailing_stop("BTCUSDT")  # 54000 - 950 = 53050

        # Sin smoothing sería 54000 - 5000 = 49000; con smoothing queda por encima de 52000.
        assert stop_after_spike > Decimal("52000")
        assert stop_after_spike is not None
        assert stop_before_spike is not None

    def test_fallback_to_seed_when_no_atr_provided(self) -> None:
        """Cuando tick() no recibe atr=, usa el último valor suavizado (o la semilla config)."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(
            PositionConfig(
                symbol="BTCUSDT",
                trailing_atr=Decimal("1000"),  # semilla
                trailing_atr_dynamic=True,
            )
        )

        # Sin ATR en tick: usa semilla = 1000 → stop = 53000 - 1000 = 52000
        pm.tick("BTCUSDT", Decimal("53000"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52000")

        # Con ATR=500: actualiza estado suavizado → stop = 54000 - 500 = 53500
        pm.tick("BTCUSDT", Decimal("54000"), atr=Decimal("500"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("53500")

        # Sin ATR en tick: mantiene último suavizado = 500 → stop = 55000 - 500 = 54500
        pm.tick("BTCUSDT", Decimal("55000"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("54500")

    def test_snapshot_atr_backward_compat(self) -> None:
        """trailing_atr sin trailing_atr_dynamic usa snapshot: ignora atr= en tick()."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_atr=Decimal("1000")))

        # atr=9999 en tick se ignora → stop = 53000 - 1000 = 52000 (snapshot)
        pm.tick("BTCUSDT", Decimal("53000"), atr=Decimal("9999"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52000")

    def test_dynamic_atr_only_no_snapshot_required(self) -> None:
        """trailing_atr_dynamic=True activa modo ATR sin necesitar trailing_atr snapshot."""
        cfg = PositionConfig(symbol="BTCUSDT", trailing_atr_dynamic=True)
        assert cfg.resolved_trailing_mode == TrailingMode.ATR

    def test_smoothing_alpha_requires_atr_mode(self) -> None:
        """trailing_atr_smoothing_alpha fuera de modo ATR lanza ValueError."""
        with pytest.raises(ValueError, match="trailing_atr_smoothing_alpha requires ATR mode"):
            PositionConfig(
                symbol="BTCUSDT",
                stop_loss=Decimal("48000"),
                trailing_atr_smoothing_alpha=Decimal("0.5"),
            )

    def test_dynamic_atr_resets_smoothed_state_on_reconfigure(self) -> None:
        """Al reconfigurar con set_config, el estado suavizado del ATR se resetea."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_atr_dynamic=True))

        pm.tick("BTCUSDT", Decimal("53000"), atr=Decimal("2000"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("51000")

        # Reconfigurar: resetea high-water, trailing-stop y smoothed ATR
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_atr_dynamic=True))
        assert pm.get_trailing_stop("BTCUSDT") is None

        # Primer tick post-reconfigure: usa el nuevo ATR desde cero
        pm.tick("BTCUSDT", Decimal("53000"), atr=Decimal("500"))
        assert pm.get_trailing_stop("BTCUSDT") == Decimal("52500")
