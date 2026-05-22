"""Normalization helpers for the feature engineering pipeline."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

_ROUND_DIGITS = 8


def clamp(value: float, minimum: float, maximum: float) -> float:
    """Clamp a numeric value into a closed interval."""
    if minimum > maximum:
        raise ValueError("minimum cannot be greater than maximum")
    return max(minimum, min(maximum, value))


def round_feature(value: float, digits: int = _ROUND_DIGITS) -> float:
    """Round floats so feature hashes are stable across equivalent inputs."""
    return round(value, digits)


def optional_float(value: float | Decimal | None, *, default: float = 0.0) -> float:
    """Convert optional numeric values to rounded floats."""
    if value is None:
        return round_feature(default)
    return round_feature(float(value))


def signal_feature(value: float | None) -> float:
    """Normalize an optional signal to [-1, 1], using 0 as neutral."""
    return round_feature(clamp(optional_float(value), -1.0, 1.0))


def score_feature(value: float | Decimal | None) -> float:
    """Normalize an optional score to [0, 1], using 0 as missing."""
    return round_feature(clamp(optional_float(value), 0.0, 1.0))


def percent_feature(value: float | Decimal | None, *, cap: float = 100.0) -> float:
    """Normalize a percent-like value by a positive cap into [0, 1]."""
    if cap <= 0:
        raise ValueError("cap must be positive")
    return round_feature(clamp(optional_float(value) / cap, 0.0, 1.0))


def enum_feature(value: Enum | str | None) -> str | None:
    """Return the stable string representation for enums used in feature payloads."""
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def canonicalize(value: Any) -> Any:
    """Convert values to JSON-stable primitives for hashing and persistence."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        return round_feature(value)
    if isinstance(value, dict):
        sorted_items = sorted(value.items(), key=lambda item: str(item[0]))
        return {str(k): canonicalize(v) for k, v in sorted_items}
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, tuple):
        return [canonicalize(item) for item in value]
    return value
