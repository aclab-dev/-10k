"""GPT Context Evaluator client — wrapper async con retry, timeout y failure policy.

Decisión 08: GPT failure bloquea new entries pero NO bloquea position
management/exits cuando deterministic_position_management_without_gpt=true.
La API key se lee exclusivamente de OPENAI_API_KEY — nunca se hardcodea ni
se incluye en logs.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from enum import StrEnum

import httpx
import structlog

from backend.core.config import FailurePolicyConfig, get_config
from backend.decision_engine.schema_guard import (
    SchemaGuardResult,
    validate_gpt_json_string,
)
from backend.decision_engine.schemas import ModelDecision

_log = structlog.get_logger(__name__)

_MAX_LOG_CHARS = 500
_DEFAULT_API_BASE_URL = "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GPTClientConfig:
    """Parámetros de conexión y retry para el cliente GPT."""

    model: str = "gpt-4o"
    timeout_seconds: float = 30.0
    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    max_tokens: int = 2000
    temperature: float = 0.1
    api_base_url: str = _DEFAULT_API_BASE_URL


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GPTRequest:
    """Prompts listos para enviar al modelo, con metadata para auditoría."""

    system_prompt: str
    user_prompt: str
    prompt_version: str = "unknown"


# ---------------------------------------------------------------------------
# Purpose — controla el failure policy (Decisión 08)
# ---------------------------------------------------------------------------


class RequestPurpose(StrEnum):
    NEW_ENTRY = "NEW_ENTRY"
    POSITION_MANAGEMENT = "POSITION_MANAGEMENT"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GPTClientError(Exception):
    """Base para errores del cliente GPT. Retryable por defecto."""


class GPTRateLimitError(GPTClientError):
    """429 Rate Limit. Lleva retry_after_seconds del header si está presente."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GPTTimeoutError(GPTClientError):
    """La llamada superó el timeout configurado."""


class GPTAuthError(GPTClientError):
    """Error de autenticación (401/403) — no reintentable."""


class GPTResponseValidationError(GPTClientError):
    """Respuesta de GPT fuera del JSON Schema de ModelDecision — no reintentable."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GPTClient:
    """Async wrapper para OpenAI Chat Completions.

    Implementa:
    - Timeout configurable por llamada.
    - Retry con backoff exponencial para errores transitorios (rate limit,
      timeout, errores de red, 5xx). Respeta el header Retry-After en 429.
    - Failure policy (Decisión 08): failure en NEW_ENTRY bloquea el ciclo;
      failure en POSITION_MANAGEMENT retorna None para que el caller use
      lógica determinística en su lugar.
    """

    def __init__(
        self,
        *,
        config: GPTClientConfig | None = None,
        failure_policy: FailurePolicyConfig | None = None,
        api_key: str | None = None,
    ) -> None:
        self._config = config or GPTClientConfig()
        self._failure_policy = failure_policy or get_config().failure_policy
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not resolved_key:
            raise GPTAuthError(
                "OPENAI_API_KEY no encontrada en el entorno. "
                "Setear la variable de entorno antes de inicializar GPTClient."
            )
        self._api_key = resolved_key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request(
        self,
        req: GPTRequest,
        purpose: RequestPurpose,
    ) -> ModelDecision | None:
        """Llama a GPT y retorna ModelDecision validado.

        Retorna None si purpose=POSITION_MANAGEMENT y la llamada falla con
        failure policy deterministic=true. Re-raise GPTClientError si
        purpose=NEW_ENTRY con gpt_failure_blocks_new_entries=true.
        """
        try:
            raw = await self._call_with_retry(req)
        except GPTClientError as exc:
            return self._apply_failure_policy(exc, purpose)

        result: SchemaGuardResult = validate_gpt_json_string(raw)
        if not result.ok:
            validation_error = GPTResponseValidationError(
                f"Respuesta GPT fuera de schema: {'; '.join(result.errors)}"
            )
            return self._apply_failure_policy(validation_error, purpose)

        assert result.decision is not None
        return result.decision

    # ------------------------------------------------------------------
    # Retry loop
    # ------------------------------------------------------------------

    async def _call_with_retry(self, req: GPTRequest) -> str:
        cfg = self._config
        last_exc: GPTClientError | None = None
        next_delay: float = 0.0

        for attempt in range(cfg.max_retries + 1):
            if attempt > 0:
                _log.info(
                    "gpt_client.retry",
                    attempt=attempt,
                    delay_seconds=next_delay,
                    model=cfg.model,
                    prompt_version=req.prompt_version,
                )
                await asyncio.sleep(next_delay)

            try:
                return await self._call_once(req)
            except (GPTAuthError, GPTResponseValidationError):
                raise  # No reintentable
            except GPTRateLimitError as exc:
                last_exc = exc
                if exc.retry_after_seconds is not None:
                    next_delay = min(exc.retry_after_seconds, cfg.max_delay_seconds)
                else:
                    next_delay = min(
                        cfg.base_delay_seconds * (2**attempt),
                        cfg.max_delay_seconds,
                    )
            except GPTClientError as exc:
                last_exc = exc
                next_delay = min(
                    cfg.base_delay_seconds * (2**attempt),
                    cfg.max_delay_seconds,
                )

        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Single HTTP call
    # ------------------------------------------------------------------

    async def _call_once(self, req: GPTRequest) -> str:
        cfg = self._config

        _log.info(
            "gpt_client.request",
            model=cfg.model,
            prompt_version=req.prompt_version,
        )

        try:
            async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
                response = await client.post(
                    f"{cfg.api_base_url}/chat/completions",
                    json={
                        "model": cfg.model,
                        "messages": [
                            {"role": "system", "content": req.system_prompt},
                            {"role": "user", "content": req.user_prompt},
                        ],
                        "max_tokens": cfg.max_tokens,
                        "temperature": cfg.temperature,
                    },
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise GPTTimeoutError(
                f"Timeout tras {cfg.timeout_seconds}s en model={cfg.model}"
            ) from exc
        except httpx.RequestError as exc:
            raise GPTClientError(f"Error de red al llamar a OpenAI: {exc}") from exc

        return self._extract_content(response, cfg.model)

    def _extract_content(self, response: httpx.Response, model: str) -> str:
        status = response.status_code

        if status == 429:
            retry_after: float | None = None
            raw_ra = response.headers.get("Retry-After")
            if raw_ra is not None:
                try:
                    retry_after = float(raw_ra)
                except ValueError:
                    pass
            raise GPTRateLimitError(
                f"Rate limit (429) en model={model}",
                retry_after_seconds=retry_after,
            )

        if status in (401, 403):
            raise GPTAuthError(
                f"Auth error {status} en model={model}: "
                f"{response.text[:_MAX_LOG_CHARS]}"
            )

        if status >= 500:
            raise GPTClientError(
                f"OpenAI server error {status} en model={model}: "
                f"{response.text[:_MAX_LOG_CHARS]}"
            )

        if status != 200:
            raise GPTClientError(
                f"Status inesperado {status} en model={model}: "
                f"{response.text[:_MAX_LOG_CHARS]}"
            )

        data = response.json()
        content: str = data["choices"][0]["message"]["content"]

        _log.info(
            "gpt_client.response",
            model=model,
            finish_reason=data["choices"][0].get("finish_reason"),
            prompt_tokens=data.get("usage", {}).get("prompt_tokens"),
            completion_tokens=data.get("usage", {}).get("completion_tokens"),
        )
        return content

    # ------------------------------------------------------------------
    # Failure policy (Decisión 08)
    # ------------------------------------------------------------------

    def _apply_failure_policy(
        self,
        exc: GPTClientError,
        purpose: RequestPurpose,
    ) -> ModelDecision | None:
        """Aplica failure policy según purpose y config.

        POSITION_MANAGEMENT con deterministic=true → retorna None (fallback
        determinístico). NEW_ENTRY con gpt_failure_blocks=true → re-raise.
        """
        policy = self._failure_policy

        if purpose == RequestPurpose.POSITION_MANAGEMENT:
            if (
                policy.exits_do_not_require_gpt_response
                and policy.deterministic_position_management_without_gpt
            ):
                _log.warning(
                    "gpt_client.failure_deterministic_fallback",
                    purpose=str(purpose),
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return None

        if (
            purpose == RequestPurpose.NEW_ENTRY
            and policy.gpt_failure_blocks_new_entries
        ):
            _log.error(
                "gpt_client.failure_blocks_entry",
                purpose=str(purpose),
                error_type=type(exc).__name__,
                error=str(exc),
            )

        raise exc
