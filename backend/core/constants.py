"""Constantes numéricas compartidas.

Viven acá para que no se redeclaren por módulo: una copia que se corrige sola
en un archivo y no en los otros cinco es exactamente el tipo de divergencia
silenciosa que este proyecto no puede permitirse en cálculos de dinero.
"""

from __future__ import annotations

from decimal import Decimal

#: Paso de cuantización de importes y precios: 8 decimales, el mismo que usan
#: las columnas Numeric(20, 8) del Anexo B.
QUANT = Decimal("0.00000001")

#: Basis points por unidad: 1 bp = 1/10 000. Dividir por esto convierte BPS a
#: fracción.
BASIS_POINTS = Decimal("10000")

#: Piso del RR neto exigido para operar (spec §3.8/§4.10, `MIN_NET_RR`). Lo
#: usan dos controles que miden cosas distintas y no deben contradecirse:
#: - `ModelDecision` filtra con él el `net_risk_reward` que *declara GPT*
#:   (autoestimación, no auditada: el JSON Schema Guard corta antes del ciclo).
#: - `RiskConfig.min_net_risk_reward` no puede quedar por debajo: es el umbral
#:   con el que el Risk Engine (`check_fee_gate`) recalcula el RR neto con las
#:   tasas reales del adapter, y es el que tiene la última palabra.
MIN_NET_RISK_REWARD_FLOOR = 1.5

__all__ = ["BASIS_POINTS", "MIN_NET_RISK_REWARD_FLOOR", "QUANT"]
