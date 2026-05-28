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
            return BudgetCheckResult(
                ok=True,
                status=BudgetStatus.NORMAL,
                tokens_used_hour=0,
                tokens_used_day=0,
                limit_hour=cfg.max_tokens_per_hour,
                limit_day=cfg.max_tokens_per_day,
            )

        now = datetime.now(UTC)
        used_hour = self._repo.tokens_in_window(self._bot_run_id, now - timedelta(hours=1))
        used_day = self._repo.tokens_in_window(self._bot_run_id, now - timedelta(hours=24))

        hour_exceeded = used_hour >= cfg.max_tokens_per_hour
        day_exceeded = used_day >= cfg.max_tokens_per_day

        if hour_exceeded or day_exceeded:
            _log.warning(
                "token_budget.exceeded",
                bot_run_id=self._bot_run_id,
                tokens_used_hour=used_hour,
                limit_hour=cfg.max_tokens_per_hour,
                tokens_used_day=used_day,
                limit_day=cfg.max_tokens_per_day,
            )
            if self._failure_policy.token_budget_failure_blocks_new_entries:
                raise TokenBudgetExceededError(
                    f"Token budget excedido: "
                    f"hora={used_hour}/{cfg.max_tokens_per_hour}, "
                    f"día={used_day}/{cfg.max_tokens_per_day}"
                )
            return BudgetCheckResult(
                ok=False,
                status=BudgetStatus.EXCEEDED,
                tokens_used_hour=used_hour,
                tokens_used_day=used_day,
                limit_hour=cfg.max_tokens_per_hour,
                limit_day=cfg.max_tokens_per_day,
            )

        alert = cfg.alert_at_percent
        near_hour = used_hour >= alert * cfg.max_tokens_per_hour
        near_day = used_day >= alert * cfg.max_tokens_per_day
        status = BudgetStatus.WARNING if (near_hour or near_day) else BudgetStatus.NORMAL

        if status == BudgetStatus.WARNING:
            _log.warning(
                "token_budget.alert",
                bot_run_id=self._bot_run_id,
                tokens_used_hour=used_hour,
                limit_hour=cfg.max_tokens_per_hour,
                tokens_used_day=used_day,
                limit_day=cfg.max_tokens_per_day,
                alert_at_percent=alert,
            )

        return BudgetCheckResult(
            ok=True,
            status=status,
            tokens_used_hour=used_hour,
            tokens_used_day=used_day,
            limit_hour=cfg.max_tokens_per_hour,
            limit_day=cfg.max_tokens_per_day,
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
        record = TokenUsage(
            bot_run_id=self._bot_run_id,
            model_request_id=model_request_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
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
