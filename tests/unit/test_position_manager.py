"""Tests unitarios del PositionManager (SL/TP/trailing stop)."""

from __future__ import annotations

from decimal import Decimal

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.position_manager import PositionConfig, PositionManager, PositionTriggerReason
from backend.position_manager.break_even import maybe_move_to_break_even
from backend.position_manager.trailing import compute_trailing_stop, is_trailing_stop_hit

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
        assert pm._trailing_stop["BTCUSDT"] == Decimal("52000")

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
        assert pm._trailing_stop["BTCUSDT"] == Decimal("48000")

        r = pm.tick("BTCUSDT", Decimal("48000"))
        assert r.trigger == PositionTriggerReason.TRAILING_SL_HIT
        assert adapter.get_position("BTCUSDT") is None

    def test_trailing_high_water_resets_on_reconfigure(self) -> None:
        """Al llamar set_config de nuevo, el high-water se resetea."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_delta=Decimal("1000")))

        # Primer tick: high-water = 55000
        pm.tick("BTCUSDT", Decimal("55000"))
        assert pm._high_water["BTCUSDT"] == Decimal("55000")

        # Reconfigurar → high-water debe desaparecer
        pm.set_config(PositionConfig(symbol="BTCUSDT", trailing_delta=Decimal("500")))
        assert "BTCUSDT" not in pm._high_water


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
