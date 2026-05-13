"""Repositorios para el agregado Market: snapshots, señales, régimen, volatilidad, features."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from backend.storage.models import (
    FeaturePackage,
    MarketRegime,
    MarketSnapshot,
    QuantSignal,
    VolatilityAssessment,
)
from backend.storage.repositories.base import BaseRepository


class MarketSnapshotRepository(BaseRepository[MarketSnapshot]):
    model = MarketSnapshot

    def get_latest_by_symbol(self, bot_run_id: str, symbol: str) -> MarketSnapshot | None:
        stmt = (
            select(MarketSnapshot)
            .where(
                MarketSnapshot.bot_run_id == bot_run_id,
                MarketSnapshot.symbol == symbol,
            )
            .order_by(MarketSnapshot.timestamp.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def list_by_symbol(
        self,
        bot_run_id: str,
        symbol: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[MarketSnapshot]:
        stmt = select(MarketSnapshot).where(
            MarketSnapshot.bot_run_id == bot_run_id,
            MarketSnapshot.symbol == symbol,
        )
        if since is not None:
            stmt = stmt.where(MarketSnapshot.timestamp >= since)
        stmt = stmt.order_by(MarketSnapshot.timestamp.desc()).limit(limit)
        return list(self._session.scalars(stmt))


class QuantSignalRepository(BaseRepository[QuantSignal]):
    model = QuantSignal

    def get_latest_by_symbol(self, bot_run_id: str, symbol: str) -> QuantSignal | None:
        stmt = (
            select(QuantSignal)
            .where(
                QuantSignal.bot_run_id == bot_run_id,
                QuantSignal.symbol == symbol,
            )
            .order_by(QuantSignal.timestamp.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()


class MarketRegimeRepository(BaseRepository[MarketRegime]):
    model = MarketRegime

    def get_latest_by_symbol(self, bot_run_id: str, symbol: str) -> MarketRegime | None:
        stmt = (
            select(MarketRegime)
            .where(
                MarketRegime.bot_run_id == bot_run_id,
                MarketRegime.symbol == symbol,
            )
            .order_by(MarketRegime.timestamp.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()


class VolatilityAssessmentRepository(BaseRepository[VolatilityAssessment]):
    model = VolatilityAssessment

    def get_latest_by_symbol(
        self, bot_run_id: str, symbol: str
    ) -> VolatilityAssessment | None:
        stmt = (
            select(VolatilityAssessment)
            .where(
                VolatilityAssessment.bot_run_id == bot_run_id,
                VolatilityAssessment.symbol == symbol,
            )
            .order_by(VolatilityAssessment.timestamp.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()


class FeaturePackageRepository(BaseRepository[FeaturePackage]):
    model = FeaturePackage

    def get_by_hash(self, features_hash: str) -> FeaturePackage | None:
        stmt = select(FeaturePackage).where(FeaturePackage.features_hash == features_hash)
        return self._session.scalars(stmt).first()

    def get_latest_by_symbol(self, bot_run_id: str, symbol: str) -> FeaturePackage | None:
        stmt = (
            select(FeaturePackage)
            .where(
                FeaturePackage.bot_run_id == bot_run_id,
                FeaturePackage.symbol == symbol,
            )
            .order_by(FeaturePackage.timestamp.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()
