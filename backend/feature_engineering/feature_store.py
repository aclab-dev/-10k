"""Persistence helpers for feature engineering packages."""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.feature_engineering.schemas import FeatureEngineeringPackage
from backend.storage.models import FeaturePackage
from backend.storage.repositories import FeaturePackageRepository


class FeatureStore:
    """Thin storage adapter for versioned feature packages."""

    def __init__(self, session: Session) -> None:
        self._repo = FeaturePackageRepository(session)

    def save(self, package: FeatureEngineeringPackage, *, bot_run_id: str) -> FeaturePackage:
        """Persist a feature package into feature_packages."""
        record = FeaturePackage(**package.to_db_kwargs(bot_run_id))
        return self._repo.save(record)

    def get_by_hash(self, features_hash: str) -> FeaturePackage | None:
        return self._repo.get_by_hash(features_hash)

    def get_latest_by_symbol(self, bot_run_id: str, symbol: str) -> FeaturePackage | None:
        return self._repo.get_latest_by_symbol(bot_run_id, symbol)
