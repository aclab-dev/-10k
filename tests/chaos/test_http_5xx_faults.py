"""Caos — respuestas 5xx (F16 [118]).

Inyecta errores de servidor en las integraciones externas (OpenAI, exchange) y
valida: reintento en el transitorio, corte en seco cuando persiste (circuit
breaker), y que la reconciliación nunca declara "consistente" un símbolo que no
pudo consultar.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.config import get_config
from backend.core.retry import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    RetryConfig,
    retry_async,
)
from backend.decision_engine.gpt_client import (
    GPTClient,
    GPTClientError,
    GPTRequest,
    RequestPurpose,
)
from backend.exchange_adapters.paper_adapter import PaperAdapter
from backend.reconciliation.engine import ReconciliationEngine
from tests.chaos.faults import ChaosAdapter, InjectedServerError

pytestmark = pytest.mark.chaos

_BOT_RUN_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# retry / circuit breaker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_5xx_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.core.retry.asyncio.sleep", AsyncMock())
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise InjectedServerError("502 Bad Gateway")
        return "ok"

    result = await retry_async(
        op,
        config=RetryConfig(max_attempts=5, base_delay_seconds=0.0, jitter_ratio=0.0),
        is_retryable=lambda _exc: True,
    )
    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_persistent_5xx_opens_circuit_breaker_and_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.core.retry.asyncio.sleep", AsyncMock())
    calls = 0

    async def op() -> str:
        nonlocal calls
        calls += 1
        raise InjectedServerError("500")

    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, reset_timeout_seconds=60.0))
    config = RetryConfig(max_attempts=1, base_delay_seconds=0.0, jitter_ratio=0.0)

    with pytest.raises(InjectedServerError):
        await retry_async(op, config=config, is_retryable=lambda _exc: True, circuit_breaker=cb)
    with pytest.raises(CircuitBreakerOpenError):
        await retry_async(op, config=config, is_retryable=lambda _exc: True, circuit_breaker=cb)

    assert calls == 1  # la segunda vuelta ni siquiera invocó la operación


# ---------------------------------------------------------------------------
# GPTClient — 5xx sostenido en NEW_ENTRY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gpt_persistent_5xx_blocks_new_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI devuelve 5xx en todos los reintentos → request() para NEW_ENTRY
    con la policy bloqueante propaga GPTClientError; nunca retorna una decisión."""
    policy = get_config().failure_policy.model_copy(update={"gpt_failure_blocks_new_entries": True})
    client = GPTClient(api_key="test-key", failure_policy=policy)
    monkeypatch.setattr("backend.core.retry.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(
        client, "_call_once", AsyncMock(side_effect=GPTClientError("OpenAI server error 503"))
    )

    req = GPTRequest(system_prompt="s", user_prompt="u", prompt_version="v1")
    with pytest.raises(GPTClientError):
        await client.request(req, RequestPurpose.NEW_ENTRY)

    await client.aclose()


# ---------------------------------------------------------------------------
# ReconciliationEngine — 5xx del exchange en un símbolo
# ---------------------------------------------------------------------------


def _repos() -> tuple[MagicMock, MagicMock]:
    position_repo = MagicMock()
    position_repo.list_open.return_value = []
    order_repo = MagicMock()
    order_repo.list_by_status.return_value = []
    order_repo.list_by_client_order_ids.return_value = []
    return position_repo, order_repo


def test_reconciliation_marks_symbol_failed_on_5xx_and_is_not_consistent() -> None:
    inner = PaperAdapter(initial_balance_usdt=Decimal("1000"))
    adapter = ChaosAdapter(inner)
    adapter.fail("get_open_orders", exc=InjectedServerError("500 from exchange"), symbol="ETHUSDT")
    adapter.fail("get_position", exc=InjectedServerError("500 from exchange"), symbol="ETHUSDT")

    position_repo, order_repo = _repos()
    engine = ReconciliationEngine(
        adapter,
        position_repo,
        order_repo,
        symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
    )

    report = engine.reconcile(_BOT_RUN_ID)

    assert "ETHUSDT" in report.failed_symbols
    assert "BTCUSDT" not in report.failed_symbols  # el símbolo sano sí se verificó
    assert report.is_complete is False
    assert report.is_consistent is False  # nunca afirma consistencia sin haber podido chequear
    # No se fabricó ninguna discrepancia para el símbolo que no se pudo leer.
    assert all(d.symbol != "ETHUSDT" for d in report.position_discrepancies)
    assert all(d.symbol != "ETHUSDT" for d in report.order_discrepancies)
