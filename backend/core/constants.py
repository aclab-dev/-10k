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

__all__ = ["BASIS_POINTS", "QUANT"]
