"""Contratos del Market Regime Engine — épica F6 (sección 4.10.x del PDF maestro).

MarketRegime define los 6 estados posibles del mercado. Es el contrato de entrada
para módulos downstream (leverage calculator, decision aggregator, feature engineering).
La implementación del clasificador es responsabilidad del engine.py de esta misma épica.
"""

from __future__ import annotations

from enum import StrEnum


class MarketRegime(StrEnum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"
    BREAKOUT = "BREAKOUT"
    UNCLEAR = "UNCLEAR"
