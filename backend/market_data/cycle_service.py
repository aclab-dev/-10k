"""MarketDataCycleService — obtiene y persiste un MarketSnapshot real por ciclo.

Puente entre `ExchangeAdapter` (estado de cuenta/posiciones/órdenes), `DataFetcher`
(obtiene el snapshot crudo del símbolo) y `MarketDataEngine` (valida frescura/
coherencia y persiste). Pensado para ser inyectado en `CycleRunner`, igual que
`PositionTickService` (F14).
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.orm import Session

from backend.exchange_adapters.base import ExchangeAdapter
from backend.market_data.engine import MarketDataEngine
from backend.market_data.fetcher import DataFetcher

log = structlog.get_logger(__name__)


class MarketDataCycleService:
    """Tickea el Market Data Engine para todos los símbolos permitidos.

    Aislamiento de fallas: un símbolo cuyo fetch falla o cuyo snapshot es
    rechazado (stale/incoherente, ver Market Data Guard en validators.py) se
    loguea como ERROR y se saltea; el resto de los símbolos igual se procesan
    en ese ciclo. Mismo criterio que `PositionTickService.tick_all()` (F14):
    un solo símbolo con problemas no debe tumbar el heartbeat del loop
    operativo ni dejar sin datos frescos a los demás símbolos.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        fetcher: DataFetcher,
        engine: MarketDataEngine,
        session: Session,
        symbols: list[str],
    ) -> None:
        self._adapter = adapter
        self._fetcher = fetcher
        self._engine = engine
        self._session = session
        self._symbols = symbols

    def tick_all(self) -> None:
        """Obtiene, valida y persiste un MarketSnapshot por símbolo. Commitea al final."""
        asyncio.run(self._tick_all_async())
        self._session.commit()

    async def _tick_all_async(self) -> None:
        account_balance_usdt = self._adapter.get_account_state().balance_usdt
        open_positions_count = sum(
            1 for symbol in self._symbols if self._adapter.get_position(symbol) is not None
        )
        active_orders_count = sum(
            len(self._adapter.get_open_orders(symbol)) for symbol in self._symbols
        )

        for symbol in self._symbols:
            try:
                snapshot = await self._fetcher.fetch_snapshot(
                    symbol,
                    account_balance_usdt,
                    open_positions_count=open_positions_count,
                    active_orders_count=active_orders_count,
                )
                self._engine.process_snapshot(snapshot)
            except Exception:
                log.error("market_data_cycle_service.tick_failed", symbol=symbol, exc_info=True)
                continue


__all__ = ["MarketDataCycleService"]
