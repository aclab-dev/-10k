"""Tests unitarios para SnapshotLoader (F11) — solo validación de schema."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.replay.schemas import SnapshotWindow

# ---------------------------------------------------------------------------
# SnapshotWindow — validación Pydantic (sin DB)
# ---------------------------------------------------------------------------


class TestSnapshotWindow:
    def test_valid_window(self) -> None:
        w = SnapshotWindow(
            symbol="BTCUSDT",
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert w.symbol == "BTCUSDT"

    def test_invalid_symbol(self) -> None:
        with pytest.raises(ValueError, match="no permitido"):
            SnapshotWindow(
                symbol="DOGEUSDT",
                period_start=datetime(2026, 1, 1, tzinfo=UTC),
                period_end=datetime(2026, 1, 2, tzinfo=UTC),
            )

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(ValueError, match="posterior"):
            SnapshotWindow(
                symbol="BTCUSDT",
                period_start=datetime(2026, 1, 2, tzinfo=UTC),
                period_end=datetime(2026, 1, 1, tzinfo=UTC),
            )

    def test_end_equal_start_raises(self) -> None:
        t = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValueError, match="posterior"):
            SnapshotWindow(symbol="ETHUSDT", period_start=t, period_end=t)

    @pytest.mark.parametrize(
        "symbol",
        ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"],
    )
    def test_all_allowed_symbols_accepted(self, symbol: str) -> None:
        w = SnapshotWindow(
            symbol=symbol,
            period_start=datetime(2026, 1, 1, tzinfo=UTC),
            period_end=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert w.symbol == symbol
