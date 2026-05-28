"""GPT Context Evaluator client — wrapper async con retry, timeout y failure policy.

Decisión 08: GPT failure bloquea new entries pero NO bloquea position
management/exits cuando deterministic_position_management_without_gpt=true.
La API key se lee exclusivamente de OPENAI_API_KEY — nunca se hardcodea ni
se incluye en logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from enum import StrEnum

import httpx
import structlog
from sqlalchemy.orm import Session

from backend.core.config import FailurePolicyConfig, get_config
from backend.decision_engine.schema_guard import (
    SchemaGuardResult,
    validate_gpt_json_string,
)
from backend.decision_engine.schemas import ModelDecision
from backend.storage.audit import audit_model_request, audit_model_response

_log = structlog.get_logger(__name__)

_MAX_LOG_CHARS = 500
_DEFAULT_API_BASE_URL = "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Internal call result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _RawCallResult:
    """Resultado interno de una llamada HTTP exitosa a OpenAI."""

    content: str
    raw_json: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    finish_reason: str | None


def _compute_request_hash(model: str, system_prompt: str, user_prompt: str) -> str:
    payload = json.dumps(
        {"model": model, "system_prompt": system_prompt, "user_prompt": user_prompt},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


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
    - Connection pooling: el AsyncClient se instancia una vez y se reutiliza.
    - Retry con backoff exponencial para errores transitorios (rate limit,
      timeout, errores de red, 5xx). Respeta el header Retry-After en 429.
    - Failure policy (Decisión 08):
      - NEW_ENTRY: failure bloquea si gpt_failure_blocks_new_entries=True;
        swallow y retorna None si es False.
      - POSITION_MANAGEMENT: retorna None (fallback determinístico) si
        exits_do_not_require_gpt_response y
        deterministic_position_management_without_gpt son True.
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
        self._http_client = httpx.AsyncClient(timeout=self._config.timeout_seconds)

    async def aclose(self) -> None:
        """Cierra el connection pool. Llamar al apagar el proceso."""
        await self._http_client.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request(
        self,
        req: GPTRequest,
        purpose: RequestPurpose,
        *,
        session: Session | None = None,
        bot_run_id: str | None = None,
        symbol: str | None = None,
        feature_package_id: str | None = None,
    ) -> ModelDecision | None:
        """Llama a GPT y retorna ModelDecision validado.

        Retorna None si la failure policy permite continuar sin GPT
        (POSITION_MANAGEMENT con deterministic=True, o NEW_ENTRY con
        gpt_failure_blocks_new_entries=False).

        Si se pasa session + bot_run_id + symbol, persiste el request y la
        response en DB para auditoría y reconstrucción completa.
        """
        do_audit = session is not None and bot_run_id is not None and symbol is not None
        model_request_record = None

        if do_audit:
            context_payload: dict[str, str] = {
                "system_prompt": req.system_prompt,
                "user_prompt": req.user_prompt,
                "prompt_version": req.prompt_version,
            }
            request_hash = _compute_request_hash(
                self._config.model, req.system_prompt, req.user_prompt
            )
            model_request_record = audit_model_request(
                session,  # type: ignore[arg-type]
                bot_run_id=bot_run_id,  # type: ignore[arg-type]
                symbol=symbol,  # type: ignore[arg-type]
                model=self._config.model,
                context=context_payload,
                request_hash=request_hash,
                feature_package_id=feature_package_id,
            )

        try:
            raw_result = await self._call_with_retry(req)
        except GPTClientError as exc:
            return self._apply_failure_policy(exc, purpose)

        result: SchemaGuardResult = validate_gpt_json_string(raw_result.content)

        if do_audit and model_request_record is not None:
            normalized = (
                result.decision.model_dump(mode="json")
                if result.ok and result.decision is not None
                else None
            )
            audit_model_response(
                session,  # type: ignore[arg-type]
                model_request_id=model_request_record.id,
                model=self._config.model,
                raw_response=raw_result.raw_json,
                normalized_response=normalized,
                prompt_tokens=raw_result.prompt_tokens,
                completion_tokens=raw_result.completion_tokens,
                total_tokens=raw_result.total_tokens,
                finish_reason=raw_result.finish_reason,
                is_valid_schema=result.ok,
            )

        if not result.ok:
            validation_error = GPTResponseValidationError(
                f"Respuesta GPT fuera de schema: {'; '.join(result.errors)}"
            )
            return self._apply_failure_policy(validation_error, purpose)

        if result.decision is None:
            raise GPTResponseValidationError(
                "SchemaGuardResult.ok=True pero decision=None — invariante roto"
            )
        return result.decision

    # ------------------------------------------------------------------
    # Retry loop
    # ------------------------------------------------------------------

    async def _call_with_retry(self, req: GPTRequest) -> _RawCallResult:
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

        if last_exc is None:
            raise GPTClientError("Retry loop terminó sin excepción ni resultado")
        raise last_exc

    # ------------------------------------------------------------------
    # Single HTTP call
    # ------------------------------------------------------------------

    async def _call_once(self, req: GPTRequest) -> _RawCallResult:
        cfg = self._config

        _log.info(
            "gpt_client.request",
            model=cfg.model,
            prompt_version=req.prompt_version,
        )

        try:
            response = await self._http_client.post(
                f"{cfg.api_base_url}/chat/completions",
                json={
                    "model": cfg.model,
                    "messages": [
                        {"role": "system", "content": req.system_prompt},
                        {"role": "user", "content": req.user_prompt},
                    ],
                    "max_tokens": cfg.max_tokens,
                    "temperature": cfg.temperature,
                    "response_format": {"type": "json_object"},
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

    def _extract_content(self, response: httpx.Response, model: str) -> _RawCallResult:
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
                f"Auth error {status} en model={model}: {response.text[:_MAX_LOG_CHARS]}"
            )

        if status >= 500:
            raise GPTClientError(
                f"OpenAI server error {status} en model={model}: {response.text[:_MAX_LOG_CHARS]}"
            )

        if status != 200:
            raise GPTClientError(
                f"Status inesperado {status} en model={model}: {response.text[:_MAX_LOG_CHARS]}"
            )

        data = response.json()
        try:
            choice = data["choices"][0]
            content: str = choice["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise GPTClientError(
                f"Respuesta OpenAI con estructura inesperada en model={model}: "
                f"{str(data)[:_MAX_LOG_CHARS]}"
            ) from exc

        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise GPTClientError(
                f"Respuesta truncada por max_tokens={self._config.max_tokens} "
                f"en model={model}. Aumentar max_tokens o reducir el contexto."
            )

        usage = data.get("usage", {})
        prompt_tokens: int | None = usage.get("prompt_tokens")
        completion_tokens: int | None = usage.get("completion_tokens")
        total_tokens: int | None = usage.get("total_tokens")

        _log.info(
            "gpt_client.response",
            model=model,
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return _RawCallResult(
            content=content,
            raw_json=json.dumps(data),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason,
        )

    # ------------------------------------------------------------------
    # Failure policy (Decisión 08)
    # ------------------------------------------------------------------

    def _apply_failure_policy(
        self,
        exc: GPTClientError,
        purpose: RequestPurpose,
    ) -> ModelDecision | None:
        """Aplica failure policy según purpose y config.

        POSITION_MANAGEMENT con deterministic=True → retorna None.
        NEW_ENTRY con gpt_failure_blocks=True → re-raise.
        NEW_ENTRY con gpt_failure_blocks=False → retorna None (swallow).
        POSITION_MANAGEMENT con policy desactivada → re-raise.
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
            # Policy desactivada → re-raise
            raise exc

        # NEW_ENTRY
        if policy.gpt_failure_blocks_new_entries:
            _log.error(
                "gpt_client.failure_blocks_entry",
                purpose=str(purpose),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise exc

        # NEW_ENTRY con blocks=False → swallow y continuar sin GPT
        _log.warning(
            "gpt_client.failure_swallowed_new_entry",
            purpose=str(purpose),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
