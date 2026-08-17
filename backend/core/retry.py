"""Utilidad de retry transversal: backoff exponencial + jitter + circuit breaker básico.

Usada por los dos integraciones externas del bot que pueden fallar de forma transitoria:
BingX (`backend/exchange_adapters/bingx_adapter.py`, `backend/market_data/bingx_fetcher.py`)
y OpenAI (`backend/decision_engine/gpt_client.py`). Solo cubre errores de **transporte**
(red, timeouts, 5xx) — la clasificación de qué excepción es reintentable la decide cada
caller vía `is_retryable`; errores de negocio (ej. BingX `code != 0`, GPT `AuthError`)
nunca deben marcarse como retryable.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto


class CircuitBreakerOpenError(Exception):
    """El circuit breaker está abierto: no se intenta la llamada."""


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Parámetros de reintento. `max_attempts` incluye el primer intento."""

    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    failure_threshold: int = 5
    reset_timeout_seconds: float = 30.0


class _CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreaker:
    """Circuit breaker básico por instancia (no thread-safe).

    Cuenta fallos consecutivos; al llegar a `failure_threshold` pasa a OPEN y
    rechaza llamadas con `CircuitBreakerOpenError` hasta que transcurre
    `reset_timeout_seconds`, momento en que pasa a HALF_OPEN y deja pasar un
    intento de prueba: éxito → CLOSED, fallo → OPEN de nuevo.

    Sin locks: asume ejecución single-threaded o que el caller serializa las
    llamadas, igual criterio que el resto de estado mutable de los adapters
    (ver `BingXAdapter._one_way_verified`).
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._state = _CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def before_call(self) -> None:
        if self._state is not _CircuitState.OPEN:
            return
        assert self._opened_at is not None
        if time.monotonic() - self._opened_at < self._config.reset_timeout_seconds:
            raise CircuitBreakerOpenError(
                f"Circuit breaker abierto: {self._consecutive_failures} fallos consecutivos"
            )
        self._state = _CircuitState.HALF_OPEN

    def on_success(self) -> None:
        self._state = _CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None

    def on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state is _CircuitState.HALF_OPEN or (
            self._consecutive_failures >= self._config.failure_threshold
        ):
            self._state = _CircuitState.OPEN
            self._opened_at = time.monotonic()


def compute_delay(attempt: int, config: RetryConfig, *, retry_after: float | None = None) -> float:
    """Delay antes del intento `attempt` (0-indexed, número de reintentos previos).

    Si `retry_after` viene dado (ej. header Retry-After de un 429), se usa tal cual
    -- sin jitter -- porque es un valor explícito del servidor, no una estimación
    nuestra que necesite desincronizarse de otros clientes.
    """
    if retry_after is not None:
        return min(retry_after, config.max_delay_seconds)
    base = min(config.base_delay_seconds * (2.0**attempt), config.max_delay_seconds)
    if config.jitter_ratio <= 0:
        return base
    jitter: float = random.uniform(-config.jitter_ratio, config.jitter_ratio)
    return max(0.0, base * (1 + jitter))


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    config: RetryConfig,
    is_retryable: Callable[[Exception], bool],
    circuit_breaker: CircuitBreaker | None = None,
    get_retry_after: Callable[[Exception], float | None] | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(config.max_attempts):
        if circuit_breaker is not None:
            circuit_breaker.before_call()
        try:
            result = await operation()
        except Exception as exc:
            last_exc = exc
            if circuit_breaker is not None:
                circuit_breaker.on_failure()
            if not is_retryable(exc) or attempt == config.max_attempts - 1:
                raise
            retry_after = get_retry_after(exc) if get_retry_after is not None else None
            delay = compute_delay(attempt, config, retry_after=retry_after)
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc)
            await asyncio.sleep(delay)
            continue
        if circuit_breaker is not None:
            circuit_breaker.on_success()
        return result
    # Inalcanzable: max_attempts >= 1 garantiza return o raise dentro del loop.
    assert last_exc is not None
    raise last_exc


def retry_sync[T](
    operation: Callable[[], T],
    *,
    config: RetryConfig,
    is_retryable: Callable[[Exception], bool],
    circuit_breaker: CircuitBreaker | None = None,
    get_retry_after: Callable[[Exception], float | None] | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(config.max_attempts):
        if circuit_breaker is not None:
            circuit_breaker.before_call()
        try:
            result = operation()
        except Exception as exc:
            last_exc = exc
            if circuit_breaker is not None:
                circuit_breaker.on_failure()
            if not is_retryable(exc) or attempt == config.max_attempts - 1:
                raise
            retry_after = get_retry_after(exc) if get_retry_after is not None else None
            delay = compute_delay(attempt, config, retry_after=retry_after)
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc)
            time.sleep(delay)
            continue
        if circuit_breaker is not None:
            circuit_breaker.on_success()
        return result
    assert last_exc is not None
    raise last_exc
