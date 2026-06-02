"""Lógica de reducción de parámetros del Risk Engine.

Calcula los valores ajustados (margin, leverage) cuando uno o más checks
requieren ADJUST_DOWN. Cada función devuelve el valor más restrictivo entre
el original y el tope aplicable.
"""

from __future__ import annotations

from decimal import Decimal

from backend.core.config import AppConfig, Environment
from backend.risk_engine.checks import leverage_cap_for_env


def compute_adjusted_margin(original: Decimal, max_margin: Decimal) -> Decimal:
    """Devuelve el menor entre el margen original y el tope configurado."""
    return min(original, max_margin)


def compute_adjusted_leverage(
    original: int,
    config: AppConfig,
    environment: Environment,
) -> int:
    """Devuelve el menor entre el leverage original y el cap del entorno."""
    cap = leverage_cap_for_env(config, environment)
    return min(original, cap)
