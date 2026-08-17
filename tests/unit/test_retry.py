"""Tests unitarios para backend.core.retry — backoff, jitter, circuit breaker."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.core.retry import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    RetryConfig,
    compute_delay,
    retry_async,
    retry_sync,
)

# ---------------------------------------------------------------------------
# compute_delay
# ---------------------------------------------------------------------------


def test_compute_delay_exponential_growth_no_jitter() -> None:
    config = RetryConfig(base_delay_seconds=1.0, max_delay_seconds=100.0, jitter_ratio=0.0)
    assert compute_delay(0, config) == 1.0
    assert compute_delay(1, config) == 2.0
    assert compute_delay(2, config) == 4.0
    assert compute_delay(3, config) == 8.0


def test_compute_delay_caps_at_max_delay() -> None:
    config = RetryConfig(base_delay_seconds=1.0, max_delay_seconds=5.0, jitter_ratio=0.0)
    assert compute_delay(10, config) == 5.0


def test_compute_delay_jitter_within_bounds() -> None:
    config = RetryConfig(base_delay_seconds=10.0, max_delay_seconds=100.0, jitter_ratio=0.2)
    for _ in range(50):
        delay = compute_delay(0, config)
        assert 8.0 <= delay <= 12.0


def test_compute_delay_retry_after_overrides_without_jitter() -> None:
    """retry_after explícito se usa tal cual, sin jitter (regla que test_gpt_client.py
    verifica indirectamente con mock_sleep.assert_called_once_with(7.0))."""
    config = RetryConfig(base_delay_seconds=1.0, max_delay_seconds=100.0, jitter_ratio=0.5)
    assert compute_delay(3, config, retry_after=7.0) == 7.0


def test_compute_delay_retry_after_capped_at_max_delay() -> None:
    config = RetryConfig(max_delay_seconds=5.0)
    assert compute_delay(0, config, retry_after=999.0) == 5.0


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, reset_timeout_seconds=60.0))
    cb.before_call()  # CLOSED, no-op
    cb.on_failure()
    cb.before_call()  # todavía cerrado (1 fallo < threshold 2)
    cb.on_failure()
    with pytest.raises(CircuitBreakerOpenError):
        cb.before_call()


def test_circuit_breaker_closes_on_success() -> None:
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=60.0))
    cb.on_failure()
    with pytest.raises(CircuitBreakerOpenError):
        cb.before_call()

    # Simular que pasó el cooldown → half-open → éxito cierra el breaker.
    cb._opened_at = cb._opened_at - 61.0  # type: ignore[operator]
    cb.before_call()  # transición a HALF_OPEN, no debe levantar
    cb.on_success()

    cb.before_call()  # CLOSED de nuevo, no debe levantar


def test_circuit_breaker_half_open_failure_reopens() -> None:
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=60.0))
    cb.on_failure()
    cb._opened_at = cb._opened_at - 61.0  # type: ignore[operator]
    cb.before_call()  # HALF_OPEN
    cb.on_failure()  # falla el intento de prueba → reabre

    with pytest.raises(CircuitBreakerOpenError):
        cb.before_call()


# ---------------------------------------------------------------------------
# retry_async / retry_sync
# ---------------------------------------------------------------------------


class _FlakyError(Exception):
    pass


class _FatalError(Exception):
    pass


@pytest.mark.asyncio
async def test_retry_async_succeeds_after_transient_failures() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _FlakyError("boom")
        return "ok"

    config = RetryConfig(max_attempts=5, base_delay_seconds=0.0, jitter_ratio=0.0)
    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await retry_async(op, config=config, is_retryable=lambda exc: True)

    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_async_stops_on_non_retryable() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        raise _FatalError("no retry")

    config = RetryConfig(max_attempts=5, base_delay_seconds=0.0, jitter_ratio=0.0)
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(_FatalError):
            await retry_async(
                op, config=config, is_retryable=lambda exc: not isinstance(exc, _FatalError)
            )

    assert calls == 1


@pytest.mark.asyncio
async def test_retry_async_exhausts_max_attempts() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        raise _FlakyError("always fails")

    config = RetryConfig(max_attempts=3, base_delay_seconds=0.0, jitter_ratio=0.0)
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(_FlakyError):
            await retry_async(op, config=config, is_retryable=lambda exc: True)

    assert calls == 3


@pytest.mark.asyncio
async def test_retry_async_invokes_on_retry_callback() -> None:
    calls = 0
    observed: list[tuple[int, float]] = []

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 2:
            raise _FlakyError("boom")
        return "ok"

    def on_retry(attempt: int, delay: float, exc: Exception) -> None:
        observed.append((attempt, delay))

    config = RetryConfig(max_attempts=5, base_delay_seconds=0.0, jitter_ratio=0.0)
    with patch("asyncio.sleep", new_callable=AsyncMock):
        await retry_async(op, config=config, is_retryable=lambda exc: True, on_retry=on_retry)

    assert observed == [(1, 0.0)]


@pytest.mark.asyncio
async def test_retry_async_circuit_breaker_blocks_after_open() -> None:
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        raise _FlakyError("boom")

    config = RetryConfig(max_attempts=1, base_delay_seconds=0.0, jitter_ratio=0.0)
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=60.0))

    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(_FlakyError):
            await retry_async(op, config=config, is_retryable=lambda exc: True, circuit_breaker=cb)
        with pytest.raises(CircuitBreakerOpenError):
            await retry_async(op, config=config, is_retryable=lambda exc: True, circuit_breaker=cb)

    assert calls == 1  # la segunda llamada nunca invocó `op`


def test_retry_sync_succeeds_after_transient_failures() -> None:
    calls = 0

    def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _FlakyError("boom")
        return "ok"

    config = RetryConfig(max_attempts=5, base_delay_seconds=0.0, jitter_ratio=0.0)
    with patch("time.sleep"):
        result = retry_sync(op, config=config, is_retryable=lambda exc: True)

    assert result == "ok"
    assert calls == 3


def test_retry_sync_stops_on_non_retryable() -> None:
    calls = 0

    def op() -> str:
        nonlocal calls
        calls += 1
        raise _FatalError("no retry")

    config = RetryConfig(max_attempts=5, base_delay_seconds=0.0, jitter_ratio=0.0)
    with patch("time.sleep"):
        with pytest.raises(_FatalError):
            retry_sync(op, config=config, is_retryable=lambda exc: not isinstance(exc, _FatalError))

    assert calls == 1


def test_retry_sync_get_retry_after_used_verbatim() -> None:
    sleeps: list[float] = []

    def op() -> str:
        raise _FlakyError("boom")

    config = RetryConfig(max_attempts=2, base_delay_seconds=1.0, jitter_ratio=0.9)
    with patch("time.sleep", side_effect=lambda s: sleeps.append(s)):
        with pytest.raises(_FlakyError):
            retry_sync(
                op,
                config=config,
                is_retryable=lambda exc: True,
                get_retry_after=lambda exc: 3.0,
            )

    assert sleeps == [3.0]
