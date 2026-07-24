"""Tests unitarios del PositionTickService (F14)."""

from __future__ import annotations

import threading
import time
from decimal import Decimal

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.exchange_adapters.schemas import OrderRequest, OrderSide, OrderType
from backend.position_manager import PositionConfig, PositionManager, PositionTriggerReason
from backend.position_manager.tick_service import PositionTickService

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


class _FlakyCloseAdapter(PaperAdapter):
    """PaperAdapter cuyas primeras N órdenes de cierre (is_reduce_only) fallan.

    Simula un exchange que rechaza la orden de cierre justo cuando dispara el
    SL — el caso que PositionManager.tick() no absorbe: su finally ya corrió
    remove_config() antes de que la excepción suba.
    """

    def __init__(self, initial_balance_usdt: Decimal, fail_closes: int = 1) -> None:
        super().__init__(initial_balance_usdt=initial_balance_usdt)
        self._remaining_failures = fail_closes

    def place_order(self, request: OrderRequest):  # type: ignore[override]
        if request.is_reduce_only and self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("simulated exchange rejection of close order")
        return super().place_order(request)


# ---------------------------------------------------------------------------
# tick_all
# ---------------------------------------------------------------------------


class TestTickAll:
    def test_no_configured_symbols_returns_empty(self) -> None:
        adapter = PaperAdapter()
        pm = PositionManager(adapter)
        calls: list[str] = []
        service = PositionTickService(pm, lambda symbol: calls.append(symbol) or Decimal("1"))

        results = service.tick_all()

        assert results == []
        assert calls == []

    def test_ticks_every_configured_symbol(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        _open_long(adapter, "ETHUSDT", Decimal("1"), Decimal("3000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("1")))
        pm.set_config(PositionConfig(symbol="ETHUSDT", stop_loss=Decimal("1")))

        prices = {"BTCUSDT": Decimal("50500"), "ETHUSDT": Decimal("3050")}
        service = PositionTickService(pm, lambda symbol: prices[symbol])

        results = service.tick_all()

        assert {r.symbol for r in results} == {"BTCUSDT", "ETHUSDT"}
        assert all(r.trigger == PositionTriggerReason.NONE for r in results)

    def test_sl_trigger_removes_symbol_from_next_cycle(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))
        service = PositionTickService(pm, lambda symbol: Decimal("47000"))

        first = service.tick_all()
        assert first[0].trigger == PositionTriggerReason.SL_HIT

        second = service.tick_all()
        assert second == []

    def test_get_mark_price_not_called_for_unconfigured_symbols(self) -> None:
        """Un símbolo con posición abierta pero sin config no se tickea."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        _open_long(adapter, "ETHUSDT", Decimal("1"), Decimal("3000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("1")))
        # ETHUSDT tiene posición abierta pero nunca se le hizo set_config.

        queried: list[str] = []

        def get_mark_price(symbol: str) -> Decimal:
            queried.append(symbol)
            return Decimal("50500")

        service = PositionTickService(pm, get_mark_price)
        service.tick_all()

        assert queried == ["BTCUSDT"]


class TestTickAllFailureIsolation:
    def test_one_symbol_failing_does_not_block_the_others(self) -> None:
        """Un símbolo que falla no debe impedir que se tickeen los demás ni
        crashear tick_all() entero (PositionManager es 100% in-memory: un crash
        del ciclo pierde el monitoreo de todas las posiciones, no solo la que
        falló)."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("5000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        _open_long(adapter, "ETHUSDT", Decimal("1"), Decimal("3000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("1")))
        pm.set_config(PositionConfig(symbol="ETHUSDT", stop_loss=Decimal("1")))

        def get_mark_price(symbol: str) -> Decimal:
            if symbol == "BTCUSDT":
                raise TimeoutError("simulated network hang")
            return Decimal("3050")

        service = PositionTickService(pm, get_mark_price)

        results = service.tick_all()

        assert {r.symbol for r in results} == {"ETHUSDT"}
        # El símbolo que falló conserva su config: se reintenta el próximo ciclo.
        assert "BTCUSDT" in pm.configured_symbols()

    def test_all_symbols_failing_returns_empty_without_raising(self) -> None:
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("1")))

        def get_mark_price(symbol: str) -> Decimal:
            raise TimeoutError("simulated network hang")

        service = PositionTickService(pm, get_mark_price)

        results = service.tick_all()

        assert results == []


class TestTickAllCloseOrderFailure:
    """Escenario reportado en el review 2 del PR #95: el SL dispara, la orden de
    cierre falla, PositionManager ya limpió la config en su finally. Sin
    re-registrar, la posición queda abierta, por debajo del SL, sin config y sin
    reintentos — silenciosa. Ver PositionTickService docstring."""

    def test_failed_close_reregisters_config_instead_of_orphaning_position(self) -> None:
        adapter = _FlakyCloseAdapter(initial_balance_usdt=Decimal("1000"), fail_closes=1)
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))
        service = PositionTickService(pm, lambda symbol: Decimal("47000"))  # dispara el SL

        results = service.tick_all()

        assert results == []
        # No quedó huérfana: sigue configurada y la posición sigue abierta.
        assert "BTCUSDT" in pm.configured_symbols()
        assert adapter.get_position("BTCUSDT") is not None

    def test_retry_succeeds_on_next_cycle_once_close_order_stops_failing(self) -> None:
        adapter = _FlakyCloseAdapter(initial_balance_usdt=Decimal("1000"), fail_closes=1)
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("48000")))
        service = PositionTickService(pm, lambda symbol: Decimal("47000"))

        first = service.tick_all()
        assert first == []

        second = service.tick_all()

        assert len(second) == 1
        assert second[0].trigger == PositionTriggerReason.SL_HIT
        assert adapter.get_position("BTCUSDT") is None
        assert "BTCUSDT" not in pm.configured_symbols()


class TestTickAllSerialization:
    def test_concurrent_calls_do_not_interleave(self) -> None:
        """Dos tick_all() disparados en paralelo no deben pisarse (lock)."""
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        _open_long(adapter, "BTCUSDT", Decimal("1"), Decimal("50000"))
        pm = PositionManager(adapter)
        pm.set_config(PositionConfig(symbol="BTCUSDT", stop_loss=Decimal("1")))

        concurrency_lock = threading.Lock()
        concurrent = 0
        max_concurrent = 0

        def get_mark_price(symbol: str) -> Decimal:
            nonlocal concurrent, max_concurrent
            with concurrency_lock:
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            time.sleep(0.05)
            with concurrency_lock:
                concurrent -= 1
            return Decimal("50500")

        service = PositionTickService(pm, get_mark_price)

        threads = [threading.Thread(target=service.tick_all) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        assert max_concurrent == 1
