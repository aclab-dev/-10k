"""Test de integración: MarketDataCycleService contra PostgreSQL real (CR).

Verifica el flujo completo no-mockeado: PaperAdapter -> MockDataFetcher ->
MarketDataEngine (validación + persistencia) para todos los símbolos
permitidos, tal como lo ejercita CycleRunner en un ciclo real.

Ejecutar con: pytest -m integration

Nota: `tick_all()` commitea la sesión (necesario en producción, donde la
sesión vive durante todo el proceso del worker). Por eso este test no confía
en el rollback de `pg_session` para aislarse — usa un `bot_run_id` único
propio, filtra todas las lecturas por ese id, y cierra el BotRun al final
(status != RUNNING) para no contaminar `BotRunRepository.get_active()` en
otros tests que comparten el mismo contenedor Postgres de sesión.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.market_data.cycle_service import MarketDataCycleService
from backend.market_data.engine import MarketDataEngine
from backend.market_data.fetcher import MockDataFetcher
from backend.market_data.schemas import CoherenceStatus, DataFreshnessStatus
from backend.storage.models import BotRun
from backend.storage.repositories.snapshots import MarketSnapshotRepository

ALLOWED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]


def _bot_run(session: Session) -> BotRun:
    run = BotRun(
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"test": True},
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    return run


@pytest.mark.integration
class TestMarketDataCycleServiceIntegration:
    def test_tick_all_persists_a_fresh_valid_snapshot_per_symbol(self, pg_session: Session) -> None:
        run = _bot_run(pg_session)
        adapter = PaperAdapter(initial_balance_usdt=Decimal("1000"))
        fetcher = MockDataFetcher(seed=42)
        engine = MarketDataEngine(pg_session, run.id)
        service = MarketDataCycleService(
            adapter=adapter,
            fetcher=fetcher,
            engine=engine,
            session=pg_session,
            symbols=ALLOWED_SYMBOLS,
        )

        try:
            service.tick_all()

            repo = MarketSnapshotRepository(pg_session)
            for symbol in ALLOWED_SYMBOLS:
                snapshot = repo.get_latest_by_symbol(run.id, symbol)
                assert snapshot is not None
                assert snapshot.extra is not None
                assert snapshot.extra["data_freshness_status"] == DataFreshnessStatus.FRESH
                assert snapshot.extra["coherence_status"] == CoherenceStatus.OK
                assert Decimal(snapshot.extra["account_balance_usdt"]) == Decimal("1000")
        finally:
            # tick_all() commitea; cerramos el run para no dejar un BotRun
            # RUNNING colgado que contamine BotRunRepository.get_active() en
            # otros tests del mismo contenedor Postgres de sesión.
            run.status = "STOPPED"
            pg_session.commit()
