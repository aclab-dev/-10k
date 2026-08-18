"""Clasificación de errores de transporte de BingX (F16), compartida entre
`backend/exchange_adapters/bingx_adapter.py` (escritura/lectura autenticada) y
`backend/market_data/bingx_fetcher.py` (datos de mercado públicos).

Vive en `backend.core` — no en `exchange_adapters` ni en `market_data` — porque
ambos módulos son verticales independientes del proyecto y ninguno debe
depender del otro solo para clasificar un fallo HTTP.
"""

from __future__ import annotations

import httpx


def is_retryable_bingx_error(exc: Exception) -> bool:
    """True solo para fallos de transporte: sin respuesta, timeout, o 5xx.

    httpx.TimeoutException es subclase de httpx.RequestError, así que un solo
    isinstance cubre ambos. httpx.HTTPStatusError es un tipo hermano (no hereda de
    RequestError) y se evalúa aparte por status_code.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.RequestError)
