"""Tests de MarketDataCycleService (CR)."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from unittest.mock import Mock

from backend.core.config import Environment, MarginType
from backend.exchange_adapters.base import ExchangeAdapter
from backend.exchange_adapters.schemas import (
    AccountState,
    OrderRequest,
    OrderResult,
    OrderSide,
    PositionState,
)
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.engine import MarketDataEngine
from backend.market_data.fetcher import DataFetcher
from backend.market_data.schemas import MarketSnapshot
from backend.market_data.validators import SnapshotRejectedError

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]


class _FakeAdapter(ExchangeAdapter):
    """Adapter minimo controlable: solo implementa lo que el service usa."""

    def __init__(
        self,
        balance_usdt: Decimal = Decimal("1000"),
        positions: dict[str, PositionState] | None = None,
        open_orders: dict[str, list[OrderResult]] | None = None,
    ) -> None:
        self._balance_usdt = balance_usdt
        self._positions = positions or {}
        self._open_orders = open_orders or {}

    @property
    def environment(self) -> Environment:
        return Environment.PAPER

    def place_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError

    def cancel_order(self, client_order_id: str) -> bool:
        raise NotImplementedError

    def get_order_status(self, client_order_id: str) -> OrderResult | None:
        raise NotImplementedError

    def get_position(self, symbol: str) -> PositionState | None:
        return self._positions.get(symbol)

    def get_open_orders(self, symbol: str) -> list[OrderResult]:
        return self._open_orders.get(symbol, [])

    def get_account_state(self) -> AccountState:
        return AccountState(
            balance_usdt=self._balance_usdt,
            equity_usdt=self._balance_usdt,
            available_margin_usdt=Decimal("0"),
            used_margin_usdt=Decimal("0"),
            is_simulated=True,
        )

    def set_leverage(self, symbol: str, leverage: int) -> None:
        raise NotImplementedError

    def set_margin_type(self, symbol: str, margin_type: MarginType) -> None:
        raise NotImplementedError


class _FakeFetcher(DataFetcher):
    """Fetcher controlable: retorna un sentinel por simbolo o levanta si esta en `failing`."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.calls: list[tuple[str, Decimal, int, int]] = []

    async def fetch_snapshot(
        self,
        symbol: str,
        account_balance_usdt: Decimal,
        open_positions_count: int = 0,
        active_orders_count: int = 0,
    ) -> MarketSnapshot:
        self.calls.append((symbol, account_balance_usdt, open_positions_count, active_orders_count))
        if symbol in self.failing:
            raise ValueError(f"fetch failed for {symbol}")
        return Mock(name=f"snapshot-{symbol}")

    def is_healthy(self) -> bool:
        return True


def _position(symbol: str) -> PositionState:
    return PositionState(
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal("100"),
        leverage=1,
        margin_usdt=Decimal("10"),
        unrealized_pnl=Decimal("0"),
        is_simulated=True,
    )


def test_tick_all_fetches_and_persists_every_symbol() -> None:
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher()
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, SYMBOLS)

    service.tick_all()

    assert {call[0] for call in fetcher.calls} == set(SYMBOLS)
    assert engine.process_snapshot.call_count == len(SYMBOLS)
    session.commit.assert_called_once()


def test_tick_all_calls_on_snapshot_hook_for_each_successfully_processed_symbol() -> None:
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher(failing={"ETHUSDT"})
    engine = Mock(spec=MarketDataEngine)
    engine.process_snapshot.side_effect = [
        SnapshotRejectedError(snapshot_id="s1", reason="stale"),
        Mock(),
    ]
    session = Mock()
    on_snapshot = Mock()
    service = MarketDataCycleService(
        adapter, fetcher, engine, session, SYMBOLS, on_snapshot=on_snapshot
    )

    service.tick_all()

    # ETHUSDT: fetch falla, nunca llega a process_snapshot. BTCUSDT: rejected por
    # el engine. BNBUSDT: unico que pasa fetch + process_snapshot exitosamente.
    assert on_snapshot.call_count == 1


def test_tick_all_without_on_snapshot_hook_still_works() -> None:
    """Compat: on_snapshot es opcional y default None."""
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher()
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, SYMBOLS)

    service.tick_all()  # no debe lanzar

    assert engine.process_snapshot.call_count == len(SYMBOLS)


def test_tick_all_computes_account_state_across_symbols() -> None:
    adapter = _FakeAdapter(
        balance_usdt=Decimal("500"),
        positions={"BTCUSDT": _position("BTCUSDT")},
        open_orders={"ETHUSDT": [Mock(spec=OrderResult)], "BNBUSDT": [Mock(spec=OrderResult)]},
    )
    fetcher = _FakeFetcher()
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, SYMBOLS)

    service.tick_all()

    for _symbol, balance, open_positions_count, active_orders_count in fetcher.calls:
        assert balance == Decimal("500")
        assert open_positions_count == 1
        assert active_orders_count == 2


def test_tick_all_isolates_fetch_failures_per_symbol() -> None:
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher(failing={"ETHUSDT"})
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, SYMBOLS)

    service.tick_all()

    # BTCUSDT y BNBUSDT se procesan igual; solo ETHUSDT se saltea.
    assert engine.process_snapshot.call_count == len(SYMBOLS) - 1
    session.commit.assert_called_once()


def test_tick_all_isolates_snapshot_rejected_per_symbol() -> None:
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher()
    engine = Mock(spec=MarketDataEngine)
    engine.process_snapshot.side_effect = [
        SnapshotRejectedError(snapshot_id="s1", reason="stale"),
        Mock(),
        Mock(),
    ]
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, SYMBOLS)

    service.tick_all()

    assert engine.process_snapshot.call_count == len(SYMBOLS)
    session.commit.assert_called_once()


class _SlowFetcher(DataFetcher):
    """Fetcher que tarda `delay_s` por símbolo — sirve para medir concurrencia."""

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def fetch_snapshot(
        self,
        symbol: str,
        account_balance_usdt: Decimal,
        open_positions_count: int = 0,
        active_orders_count: int = 0,
    ) -> MarketSnapshot:
        await asyncio.sleep(self._delay_s)
        return Mock(name=f"snapshot-{symbol}")

    def is_healthy(self) -> bool:
        return True


def test_tick_all_fetches_symbols_concurrently_not_sequentially() -> None:
    """El fetch por símbolo debe correr concurrente (asyncio.gather), no uno a uno.

    Con 5 símbolos y 50ms por fetch: secuencial tardaría ~250ms, concurrente ~50ms.
    Umbral generoso (150ms) para evitar flakiness sin dejar de detectar una
    regresión a fetch secuencial.
    """
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
    adapter = _FakeAdapter()
    fetcher = _SlowFetcher(delay_s=0.05)
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, symbols)

    start = time.monotonic()
    service.tick_all()
    elapsed = time.monotonic() - start

    assert elapsed < 0.15
    assert engine.process_snapshot.call_count == len(symbols)


def _snapshot(symbol: str, last_price: Decimal) -> MarketSnapshot:
    snap = Mock(spec=MarketSnapshot)
    snap.symbol = symbol
    snap.last_price = last_price
    return snap


def test_get_last_price_is_none_before_any_tick() -> None:
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher()
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, SYMBOLS)

    assert service.get_last_price("BTCUSDT") is None


def test_get_last_price_updates_after_successful_tick() -> None:
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher()
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, ["BTCUSDT"])
    fetcher.fetch_snapshot = _make_fetch_snapshot({"BTCUSDT": Decimal("50000")})  # type: ignore[method-assign]

    service.tick_all()

    assert service.get_last_price("BTCUSDT") == Decimal("50000")


def test_get_last_price_stays_stale_when_fetch_fails_next_cycle() -> None:
    """Un fallo transitorio de fetch no debe borrar el ultimo precio conocido —
    una posicion abierta con SL/trailing activo necesita seguir teniendo un
    precio de referencia, aunque este un ciclo desactualizado."""
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher()
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, ["BTCUSDT"])
    fetcher.fetch_snapshot = _make_fetch_snapshot({"BTCUSDT": Decimal("50000")})  # type: ignore[method-assign]
    service.tick_all()
    assert service.get_last_price("BTCUSDT") == Decimal("50000")

    fetcher.fetch_snapshot = _make_failing_fetch_snapshot()  # type: ignore[method-assign]
    service.tick_all()

    assert service.get_last_price("BTCUSDT") == Decimal("50000")


def _make_fetch_snapshot(prices: dict[str, Decimal]):
    async def _fetch(
        symbol: str,
        account_balance_usdt: Decimal,
        open_positions_count: int = 0,
        active_orders_count: int = 0,
    ) -> MarketSnapshot:
        return _snapshot(symbol, prices[symbol])

    return _fetch


def _make_failing_fetch_snapshot():
    async def _fetch(
        symbol: str,
        account_balance_usdt: Decimal,
        open_positions_count: int = 0,
        active_orders_count: int = 0,
    ) -> MarketSnapshot:
        raise ValueError(f"fetch failed for {symbol}")

    return _fetch


def test_tick_all_with_no_symbols_still_commits() -> None:
    adapter = _FakeAdapter()
    fetcher = _FakeFetcher()
    engine = Mock(spec=MarketDataEngine)
    session = Mock()
    service = MarketDataCycleService(adapter, fetcher, engine, session, [])

    service.tick_all()

    engine.process_snapshot.assert_not_called()
    session.commit.assert_called_once()
