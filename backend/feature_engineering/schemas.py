"""Versioned feature package contract for the F6 feature engineering layer."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.feature_engineering.normalization import canonicalize
from backend.market_data.schemas import _ALLOWED_SYMBOLS

FEATURE_PACKAGE_VERSION = "1.0"


def compute_features_hash(
    features: dict[str, Any],
    *,
    version: str = FEATURE_PACKAGE_VERSION,
    source_versions: dict[str, str] | None = None,
) -> str:
    """Return a deterministic SHA-256 hash for a versioned feature payload."""
    payload = {
        "features": canonicalize(features),
        "source_versions": canonicalize(source_versions or {}),
        "version": version,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class FeatureEngineeringPackage(BaseModel):
    """Immutable output of the feature engineering pipeline.

    The `features_hash` is derived from version + features + source versions so
    downstream code can detect semantic changes in the feature payload.
    """

    package_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    snapshot_id: str
    symbol: str
    timestamp_utc: datetime
    version: str = FEATURE_PACKAGE_VERSION
    features: dict[str, Any]
    features_hash: str | None = None
    source_versions: dict[str, str] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}

    def model_post_init(self, __context: Any) -> None:
        if self.features_hash is None:
            object.__setattr__(
                self,
                "features_hash",
                compute_features_hash(
                    self.features,
                    version=self.version,
                    source_versions=self.source_versions,
                ),
            )

    @field_validator("package_id", "snapshot_id")
    @classmethod
    def must_be_valid_uuid(cls, value: str) -> str:
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise ValueError(f"'{value}' is not a valid UUID") from exc
        return value

    @field_validator("symbol")
    @classmethod
    def symbol_allowed(cls, value: str) -> str:
        if value not in _ALLOWED_SYMBOLS:
            raise ValueError(f"Symbol '{value}' not allowed. Valid: {sorted(_ALLOWED_SYMBOLS)}")
        return value

    @field_validator("timestamp_utc")
    @classmethod
    def must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp_utc must be UTC")
        return value

    @field_validator("features_hash")
    @classmethod
    def hash_must_be_sha256(cls, value: str | None) -> str | None:
        invalid_hash = value is not None and (
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
        )
        if invalid_hash:
            raise ValueError("features_hash must be a lowercase SHA-256 hex digest")
        return value

    def to_db_kwargs(self, bot_run_id: str) -> dict[str, Any]:
        """Return kwargs compatible with storage.models.FeaturePackage."""
        return {
            "id": self.package_id,
            "bot_run_id": bot_run_id,
            "market_snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "timestamp": self.timestamp_utc,
            "version": self.version,
            "features": canonicalize(self.features),
            "features_hash": self.features_hash,
        }
