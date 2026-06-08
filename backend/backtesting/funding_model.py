"""Modelo de funding para backtesting.

La función pura `compute_funding_payment` vive en `backend.core.funding`
para que pueda ser compartida por el PaperAdapter, el live engine y el
risk manager sin dependencias circulares.

Este módulo re-exporta la función por compatibilidad y está pensado para
crecer con lógica exclusiva de backtesting (slippage de funding histórico,
interpolación de tasas, etc.).
"""

from backend.core.funding import compute_funding_payment

__all__ = ["compute_funding_payment"]
