"""MarketDataCycleService — obtiene y persiste un MarketSnapshot real por ciclo.

Puente entre `ExchangeAdapter` (estado de cuenta/posiciones/órdenes), `DataFetcher`
(obtiene el snapshot crudo del símbolo) y `MarketDataEngine` (valida frescura/
coherencia y persiste). Pensado para ser inyectado en `CycleRunner`, igual que
`PositionTickService` (F14).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import structlog
from sqlalchemy.orm import Session

from backend.exchange_adapters.base import ExchangeAdapter
from backend.market_data.engine import MarketDataEngine
from backend.market_data.fetcher import DataFetcher
from backend.market_data.schemas import MarketSnapshot

log = structlog.get_logger(__name__)


class MarketDataCycleService:
    """Tickea el Market Data Engine para todos los símbolos permitidos.

    Aislamiento de fallas: un símbolo cuyo fetch falla o cuyo snapshot es
    rechazado (stale/incoherente, ver Market Data Guard en validators.py) se
    loguea como ERROR y se saltea; el resto de los símbolos igual se procesan
    en ese ciclo. Mismo criterio que `PositionTickService.tick_all()` (F14):
    un solo símbolo con problemas no debe tumbar el heartbeat del loop
    operativo ni dejar sin datos frescos a los demás símbolos.

    `on_snapshot`, si se inyecta, se invoca con cada MarketSnapshot ya validado
    y persistido (p.ej. `MarketAnalysisService.on_snapshot`, F5/F6). Debe aislar
    sus propias fallas — no se envuelve acá en try/except adicional.
    """

    def __init__(
        self,
        adapter: ExchangeAdapter,
        fetcher: DataFetcher,
        engine: MarketDataEngine,
        session: Session,
        symbols: list[str],
        on_snapshot: Callable[[MarketSnapshot], None] | None = None,
    ) -> None:
        self._adapter = adapter
        self._fetcher = fetcher
        self._engine = engine
        self._session = session
        self._symbols = symbols
        self._on_snapshot = on_snapshot

    def tick_all(self) -> list[MarketSnapshot]:
        """Obtiene, valida y persiste un MarketSnapshot por símbolo. Commitea al final.

        Retorna la lista de snapshots que se procesaron exitosamente, para que
        el caller (CycleRunner) pueda ejecutar el pipeline de decisión sobre ellos.
        """
        snapshots = asyncio.run(self._tick_all_async())
        self._session.commit()
        return snapshots

    async def _tick_all_async(self) -> list[MarketSnapshot]:
        account_balance_usdt = self._adapter.get_account_state().balance_usdt
        open_positions_count = sum(
            1 for symbol in self._symbols if self._adapter.get_position(symbol) is not None
        )
        active_orders_count = sum(
            len(self._adapter.get_open_orders(symbol)) for symbol in self._symbols
        )

        # Fetch concurrente por símbolo (I/O-bound, se beneficia con fetchers reales
        # como BingXDataFetcher). process_snapshot() NO se paraleliza: escribe sobre
        # la misma Session de SQLAlchemy, que no es segura para uso concurrente.
        fetch_results = await asyncio.gather(
            *(
                self._fetcher.fetch_snapshot(
                    symbol,
                    account_balance_usdt,
                    open_positions_count=open_positions_count,
                    active_orders_count=active_orders_count,
                )
                for symbol in self._symbols
            ),
            return_exceptions=True,
        )

        successful: list[MarketSnapshot] = []
        for symbol, result in zip(self._symbols, fetch_results, strict=True):
            if isinstance(result, BaseException):
                log.error(
                    "market_data_cycle_service.fetch_failed",
                    symbol=symbol,
                    exc_info=result,
                )
                continue
            try:
                self._engine.process_snapshot(result)
            except Exception:
                log.error("market_data_cycle_service.process_failed", symbol=symbol, exc_info=True)
                continue
            if self._on_snapshot is not None:
                self._on_snapshot(result)
            successful.append(result)
        return successful


__all__ = ["MarketDataCycleService"]
