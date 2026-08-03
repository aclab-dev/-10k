"""Token Budget Manager — control de tokens por request y por ventana de tiempo.

Persiste en token_usage, alerta cuando se acerca al límite y bloquea nuevas
entradas cuando se excede (según failure_policy.token_budget_failure_blocks_new_entries).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.orm import Session

from backend.core.config import FailurePolicyConfig, TokenBudgetConfig, get_config
from backend.decision_engine.gpt_client import RequestPurpose
from backend.storage.models import TokenUsage
from backend.storage.repositories.audit import TokenUsageRepository
from backend.token_budget.schemas import BudgetCheckResult, BudgetStatus

_log = structlog.get_logger(__name__)


class TokenBudgetExceededError(Exception):
    """Budget de tokens excedido y la política bloquea nuevas entradas."""


class TokenBudgetManager:
    """Controla el presupuesto de tokens GPT para un bot_run.

    - check_budget(): valida antes de llamar a GPT; puede levantar
      TokenBudgetExceededError para NEW_ENTRY si el budget está agotado y la
      política de fallo lo requiere.
    - record_usage(): persiste el consumo real después de una llamada exitosa.
    """

    def __init__(
        self,
        *,
        session: Session,
        bot_run_id: str,
        config: TokenBudgetConfig | None = None,
        failure_policy: FailurePolicyConfig | None = None,
    ) -> None:
        self._repo = TokenUsageRepository(session)
        self._bot_run_id = bot_run_id
        cfg = get_config()
        self._config = config or cfg.token_budget
        self._failure_policy = failure_policy or cfg.failure_policy

    # ------------------------------------------------------------------
    # Check pre-llamada
    # ------------------------------------------------------------------

    def check_budget(self, purpose: RequestPurpose) -> BudgetCheckResult:
        """Verifica el presupuesto antes de llamar a GPT.

        POSITION_MANAGEMENT nunca es bloqueado por presupuesto (Decisión 08).
        NEW_ENTRY: levanta TokenBudgetExceededError si el budget está agotado y
        failure_policy.token_budget_failure_blocks_new_entries=True.
        """
        cfg = self._config

        if purpose == RequestPurpose.POSITION_MANAGEMENT:
            # Decisión 08: position management nunca es bloqueado por presupuesto.
            # tokens_used_hour/day se dejan en 0 intencionalmente — no se consulta
            # la DB porque esos valores no son significativos para este purpose y
            # la llamada debe ser lo más rápida posible.
            return BudgetCheckResult(
                ok=True,
                status=BudgetStatus.NORMAL,
                tokens_used_hour=0,
                tokens_used_day=0,
                limit_hour=cfg.max_tokens_per_hour,
                limit_day=cfg.max_tokens_per_24h,
            )

        now = datetime.now(UTC)
        used_hour = self._repo.tokens_in_window(self._bot_run_id, now - timedelta(hours=1))
        # Ventana deslizante de 24h (no día calendario) — ver max_tokens_per_24h en config.
        used_day = self._repo.tokens_in_window(self._bot_run_id, now - timedelta(hours=24))

        result_status = self._status_for_usage(used_hour, used_day)

        if result_status == BudgetStatus.EXCEEDED:
            _log.warning(
                "token_budget.exceeded",
                bot_run_id=self._bot_run_id,
                tokens_used_hour=used_hour,
                limit_hour=cfg.max_tokens_per_hour,
                tokens_used_day=used_day,
                limit_day=cfg.max_tokens_per_24h,
            )
            if self._failure_policy.token_budget_failure_blocks_new_entries:
                raise TokenBudgetExceededError(
                    f"Token budget excedido: "
                    f"hora={used_hour}/{cfg.max_tokens_per_hour}, "
                    f"día={used_day}/{cfg.max_tokens_per_24h}"
                )
            return BudgetCheckResult(
                ok=False,
                status=BudgetStatus.EXCEEDED,
                tokens_used_hour=used_hour,
                tokens_used_day=used_day,
                limit_hour=cfg.max_tokens_per_hour,
                limit_day=cfg.max_tokens_per_24h,
            )

        if result_status == BudgetStatus.WARNING:
            _log.warning(
                "token_budget.alert",
                bot_run_id=self._bot_run_id,
                tokens_used_hour=used_hour,
                limit_hour=cfg.max_tokens_per_hour,
                tokens_used_day=used_day,
                limit_day=cfg.max_tokens_per_24h,
                alert_at_percent=cfg.alert_at_percent,
            )

        return BudgetCheckResult(
            ok=True,
            status=result_status,
            tokens_used_hour=used_hour,
            tokens_used_day=used_day,
            limit_hour=cfg.max_tokens_per_hour,
            limit_day=cfg.max_tokens_per_24h,
        )

    def _status_for_usage(self, used_hour: int, used_day: int) -> BudgetStatus:
        """Evaluación pura de umbrales, sin side-effects (logging/excepciones)."""
        cfg = self._config
        if used_hour >= cfg.max_tokens_per_hour or used_day >= cfg.max_tokens_per_24h:
            return BudgetStatus.EXCEEDED
        alert = cfg.alert_at_percent
        near_hour = used_hour >= alert * cfg.max_tokens_per_hour
        near_day = used_day >= alert * cfg.max_tokens_per_24h
        if near_hour or near_day:
            return BudgetStatus.WARNING
        return BudgetStatus.NORMAL

    # ------------------------------------------------------------------
    # Lectura de solo estado (dashboard)
    # ------------------------------------------------------------------

    def get_current_status(self) -> BudgetCheckResult:
        """Estado actual del budget, de solo lectura.

        A diferencia de check_budget(), nunca levanta TokenBudgetExceededError ni
        loguea — está pensado para un endpoint GET del dashboard que solo quiere
        mostrar el consumo hora/día contra los límites configurados.
        """
        cfg = self._config
        now = datetime.now(UTC)
        used_hour = self._repo.tokens_in_window(self._bot_run_id, now - timedelta(hours=1))
        used_day = self._repo.tokens_in_window(self._bot_run_id, now - timedelta(hours=24))
        result_status = self._status_for_usage(used_hour, used_day)
        return BudgetCheckResult(
            ok=result_status != BudgetStatus.EXCEEDED,
            status=result_status,
            tokens_used_hour=used_hour,
            tokens_used_day=used_day,
            limit_hour=cfg.max_tokens_per_hour,
            limit_day=cfg.max_tokens_per_24h,
        )

    # ------------------------------------------------------------------
    # Registro post-llamada
    # ------------------------------------------------------------------

    def record_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
        model_request_id: str | None = None,
    ) -> TokenUsage:
        """Persiste el consumo de tokens de una llamada GPT exitosa."""
        total = prompt_tokens + completion_tokens
        if total > self._config.max_tokens_per_request:
            _log.warning(
                "token_budget.request_limit_exceeded",
                bot_run_id=self._bot_run_id,
                model=model,
                total_tokens=total,
                max_tokens_per_request=self._config.max_tokens_per_request,
            )
        record = TokenUsage(
            bot_run_id=self._bot_run_id,
            model_request_id=model_request_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
        )
        saved = self._repo.save(record)
        _log.info(
            "token_budget.recorded",
            bot_run_id=self._bot_run_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=saved.total_tokens,
        )
        return saved
