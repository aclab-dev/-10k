"""Calculadora de leverage dinámico — épica F6, tarjeta [57].

Combina el leverage_cap de volatilidad, el régimen de mercado y el entorno
para producir un leverage sugerido que respeta los topes no negociables:
  PAPER ≤10x · TESTNET ≤5x · LIVE absoluto ≤5x
"""

from __future__ import annotations

import math
from typing import Any, Final

from pydantic import BaseModel, Field

from backend.core.config import Environment, LeverageConfig
from backend.market_regime.schemas import MarketRegime
from backend.volatility.schemas import VolatilityAssessmentPackage

# ---------------------------------------------------------------------------
# Calibración
# ---------------------------------------------------------------------------

# Multiplicadores por régimen: reducen (o mantienen) el leverage_cap de volatilidad.
# Valores menores a 1.0 indican regímenes con mayor incertidumbre o peligro de spike.
_REGIME_MULTIPLIERS: Final[dict[MarketRegime, float]] = {
    MarketRegime.TRENDING: 1.0,  # dirección definida → vol cap es suficiente
    MarketRegime.RANGING: 0.8,  # sin dirección clara → leve reducción
    MarketRegime.HIGH_VOL: 0.7,  # régimen volátil sobre vol assessment → doble precaución
    MarketRegime.LOW_VOL: 1.0,  # calmo → mantener vol cap
    MarketRegime.BREAKOUT: 0.7,  # spikes impredecibles, falsas rupturas frecuentes
    MarketRegime.UNCLEAR: 0.5,  # sin señal definida → postura más conservadora
}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class DynamicLeverageResult(BaseModel):
    """Resultado de la calculadora de leverage dinámico.

    Inmutable. Contiene el leverage sugerido y el breakdown completo
    para auditoría y trazabilidad.
    """

    suggested_leverage: int = Field(ge=1)
    vol_cap: int = Field(ge=1)
    regime_adjusted: int = Field(ge=1)
    env_cap: int = Field(ge=1)
    regime: MarketRegime
    environment: Environment
    details: dict[str, Any]

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_cap(environment: Environment, cfg: LeverageConfig) -> int:
    """Devuelve el tope duro de leverage para el entorno dado."""
    if environment == Environment.PAPER:
        return cfg.max_leverage_paper
    if environment == Environment.TESTNET:
        return cfg.max_leverage_testnet
    # LIVE: usar el tope absoluto (el inicial se aplica externamente según fase)
    return cfg.max_leverage_live_absolute


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------


def compute_dynamic_leverage(
    vol_assessment: VolatilityAssessmentPackage,
    regime: MarketRegime,
    environment: Environment,
    leverage_config: LeverageConfig,
) -> DynamicLeverageResult:
    """Calcula el leverage sugerido combinando volatilidad, régimen y entorno.

    Algoritmo:
      1. vol_cap = leverage_cap del VolatilityAssessmentPackage.
      2. regime_adjusted = floor(vol_cap × multiplier_de_régimen), mínimo 1.
      3. env_cap = tope duro del entorno (PAPER/TESTNET/LIVE).
      4. suggested_leverage = min(regime_adjusted, env_cap).

    Determinístico: misma entrada → misma salida.
    """
    vol_cap = vol_assessment.leverage_cap
    multiplier = _REGIME_MULTIPLIERS[regime]
    regime_adjusted = max(1, math.floor(vol_cap * multiplier))
    env_cap = _env_cap(environment, leverage_config)
    suggested = min(regime_adjusted, env_cap)

    details: dict[str, Any] = {
        "vol_cap": vol_cap,
        "regime_multiplier": multiplier,
        "regime_adjusted_pre_floor": vol_cap * multiplier,
        "regime_adjusted": regime_adjusted,
        "env_cap": env_cap,
        "assessment_id": vol_assessment.assessment_id,
        "volatility_score": vol_assessment.volatility_score,
        "volatility_regime": vol_assessment.volatility_regime,
    }

    return DynamicLeverageResult(
        suggested_leverage=suggested,
        vol_cap=vol_cap,
        regime_adjusted=regime_adjusted,
        env_cap=env_cap,
        regime=regime,
        environment=environment,
        details=details,
    )
