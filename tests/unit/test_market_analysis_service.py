"""Tests de MarketAnalysisService (F5/F6)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock

from backend.market_data.analysis_service import MarketAnalysisService
from backend.market_data.fetcher import MockDataFetcher
from backend.storage.models import MarketRegime, QuantSignal, VolatilityAssessment
from backend.storage.repositories.snapshots import (
    MarketRegimeRepository,
    QuantSignalRepository,
    VolatilityAssessmentRepository,
)


async def _snapshot(symbol: str = "BTCUSDT"):
    fetcher = MockDataFetcher(seed=42)
    return await fetcher.fetch_snapshot(symbol, Decimal("1000"))


async def test_on_snapshot_computes_and_persists_all_three() -> None:
    snapshot = await _snapshot()
    quant_repo = Mock(spec=QuantSignalRepository)
    regime_repo = Mock(spec=MarketRegimeRepository)
    volatility_repo = Mock(spec=VolatilityAssessmentRepository)
    service = MarketAnalysisService(session=Mock(), bot_run_id="run-1")
    service._quant_repo = quant_repo  # type: ignore[attr-defined]
    service._regime_repo = regime_repo  # type: ignore[attr-defined]
    service._volatility_repo = volatility_repo  # type: ignore[attr-defined]

    service.on_snapshot(snapshot)

    assert quant_repo.save.call_count == 1
    assert isinstance(quant_repo.save.call_args[0][0], QuantSignal)
    assert regime_repo.save.call_count == 1
    assert isinstance(regime_repo.save.call_args[0][0], MarketRegime)
    assert volatility_repo.save.call_count == 1
    assert isinstance(volatility_repo.save.call_args[0][0], VolatilityAssessment)


async def test_on_snapshot_isolates_failures() -> None:
    """Una falla al persistir no debe propagar — se loguea y se traga."""
    snapshot = await _snapshot()
    quant_repo = Mock(spec=QuantSignalRepository)
    quant_repo.save.side_effect = RuntimeError("db down")
    service = MarketAnalysisService(session=Mock(), bot_run_id="run-1")
    service._quant_repo = quant_repo  # type: ignore[attr-defined]

    service.on_snapshot(snapshot)  # no debe lanzar
