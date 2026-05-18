"""Tests unitarios del MarketDataEngine (F4).

Verifican la orquestación validate → persist usando mocks, sin necesidad de
base de datos real. El comportamiento end-to-end contra PostgreSQL está en
tests/integration/test_market_data_engine.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from backend.core.config import Environment
from backend.market_data.engine import MarketDataEngine
from backend.market_data.schemas import (
    CandleData,
    Candles,
    CoherenceStatus,
    DataFreshnessStatus,
    Exchange,
)
from backend.market_data.schemas import MarketSnapshot as MarketSnapshotSchema
from backend.market_data.validators import SnapshotRejectedError
from backend.storage.models import MarketSnapshot as MarketSnapshotORM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle() -> CandleData:
    return CandleData(
        open=Decimal("50000"),
        high=Decimal("50100"),
        low=Decimal("49900"),
        close=Decimal("50010"),
        volume=Decimal("100"),
        n_candles=10,
    )


def _snapshot(**overrides: object) -> MarketSnapshotSchema:
    now = datetime.now(UTC)
    defaults: dict[str, object] = {
        "timestamp_utc": now,
        "exchange": Exchange.BINGX,
        "environment": Environment.PAPER,
        "symbol": "BTCUSDT",
        "last_price": Decimal("50005"),
        "bid": Decimal("50000"),
        "ask": Decimal("50010"),
        "spread_absolute": Decimal("10"),
        "spread_percent": Decimal("0.02"),
        "candles": Candles(tf_5m=_candle(), tf_15m=_candle(), tf_1h=_candle(), tf_4h=_candle()),
        "volume": Decimal("1000"),
        "account_balance_usdt": Decimal("500"),
        "open_positions_count": 0,
        "active_orders_count": 0,
        "latency_ms": 50,
        "exchange_server_time": now,
        "local_time": now,
        "clock_skew_ms": 10,
        "data_freshness_status": DataFreshnessStatus.FRESH,
        "coherence_status": CoherenceStatus.OK,
    }
    defaults.update(overrides)
    return MarketSnapshotSchema(**defaults)  # type: ignore[arg-type]


def _make_engine(bot_run_id: str = "run-id-123") -> tuple[MarketDataEngine, MagicMock]:
    """Devuelve (engine, mock_repo) con el repositorio reemplazado por un mock."""
    engine = MarketDataEngine(MagicMock(), bot_run_id)
    mock_repo = MagicMock()
    engine._repo = mock_repo
    return engine, mock_repo


# ---------------------------------------------------------------------------
# MarketDataEngine
# ---------------------------------------------------------------------------


class TestMarketDataEngine:
    def test_validate_snapshot_is_called(self) -> None:
        snap = _snapshot()
        engine, mock_repo = _make_engine()
        mock_repo.save_snapshot.return_value = MagicMock(spec=MarketSnapshotORM)

        with patch(
            "backend.market_data.engine.validate_snapshot", return_value=snap
        ) as mock_validate:
            engine.process_snapshot(snap)

        mock_validate.assert_called_once_with(snap)

    def test_save_snapshot_called_with_validated_result(self) -> None:
        snap = _snapshot()
        validated = _snapshot(symbol="ETHUSDT")
        engine, mock_repo = _make_engine(bot_run_id="test-run-42")
        mock_repo.save_snapshot.return_value = MagicMock(spec=MarketSnapshotORM)

        with patch("backend.market_data.engine.validate_snapshot", return_value=validated):
            engine.process_snapshot(snap)

        mock_repo.save_snapshot.assert_called_once_with(validated, "test-run-42")

    def test_returns_orm_from_repository(self) -> None:
        snap = _snapshot()
        engine, mock_repo = _make_engine()
        expected_orm = MagicMock(spec=MarketSnapshotORM)
        mock_repo.save_snapshot.return_value = expected_orm

        with patch("backend.market_data.engine.validate_snapshot", return_value=snap):
            result = engine.process_snapshot(snap)

        assert result is expected_orm

    def test_rejected_snapshot_propagates_error(self) -> None:
        snap = _snapshot()
        engine, mock_repo = _make_engine()

        with patch(
            "backend.market_data.engine.validate_snapshot",
            side_effect=SnapshotRejectedError(snapshot_id="abc-123", reason="expirado"),
        ):
            with pytest.raises(SnapshotRejectedError, match="expirado"):
                engine.process_snapshot(snap)

    def test_save_not_called_when_validation_fails(self) -> None:
        snap = _snapshot()
        engine, mock_repo = _make_engine()

        with patch(
            "backend.market_data.engine.validate_snapshot",
            side_effect=SnapshotRejectedError(snapshot_id="abc-123", reason="incoherentes"),
        ):
            with pytest.raises(SnapshotRejectedError):
                engine.process_snapshot(snap)

        mock_repo.save_snapshot.assert_not_called()

    def test_bot_run_id_forwarded_to_repository(self) -> None:
        snap = _snapshot()
        engine, mock_repo = _make_engine(bot_run_id="specific-run-99")
        mock_repo.save_snapshot.return_value = MagicMock(spec=MarketSnapshotORM)

        with patch("backend.market_data.engine.validate_snapshot", return_value=snap):
            engine.process_snapshot(snap)

        call_args = mock_repo.save_snapshot.call_args[0]
        assert call_args[1] == "specific-run-99"
