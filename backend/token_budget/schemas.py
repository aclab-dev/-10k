"""Schemas del Token Budget Manager."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BudgetStatus(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    EXCEEDED = "EXCEEDED"


@dataclass(frozen=True)
class BudgetCheckResult:
    ok: bool
    status: BudgetStatus
    tokens_used_hour: int
    tokens_used_day: int
    limit_hour: int
    limit_day: int
