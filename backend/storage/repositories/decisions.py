"""Repositorios para el agregado Decision: ModelRequest, ModelResponse, Decision,
DecisionAggregation, RiskValidation."""

from __future__ import annotations

from sqlalchemy import select

from backend.storage.models import (
    Decision,
    DecisionAggregation,
    ModelRequest,
    ModelResponse,
    RiskValidation,
)
from backend.storage.repositories.base import BaseRepository


class ModelRequestRepository(BaseRepository[ModelRequest]):
    model = ModelRequest

    def get_by_hash(self, request_hash: str) -> ModelRequest | None:
        stmt = select(ModelRequest).where(ModelRequest.request_hash == request_hash)
        return self._session.scalars(stmt).first()


class ModelResponseRepository(BaseRepository[ModelResponse]):
    model = ModelResponse

    def get_by_request_id(self, model_request_id: str) -> ModelResponse | None:
        stmt = select(ModelResponse).where(ModelResponse.model_request_id == model_request_id)
        return self._session.scalars(stmt).first()

    def list_by_bot_run(
        self, bot_run_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[ModelResponse]:
        """ModelResponse no tiene bot_run_id directo; se une vía model_request."""
        stmt = (
            select(ModelResponse)
            .join(ModelRequest, ModelResponse.model_request_id == ModelRequest.id)
            .where(ModelRequest.bot_run_id == bot_run_id)
            .order_by(ModelResponse.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(stmt))


class DecisionRepository(BaseRepository[Decision]):
    model = Decision

    def list_by_symbol(self, bot_run_id: str, symbol: str, *, limit: int = 100) -> list[Decision]:
        stmt = (
            select(Decision)
            .where(Decision.bot_run_id == bot_run_id, Decision.symbol == symbol)
            .order_by(Decision.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def list_by_action(self, bot_run_id: str, action: str, *, limit: int = 100) -> list[Decision]:
        stmt = (
            select(Decision)
            .where(Decision.bot_run_id == bot_run_id, Decision.action == action)
            .order_by(Decision.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class DecisionAggregationRepository(BaseRepository[DecisionAggregation]):
    model = DecisionAggregation

    def get_by_decision_id(self, decision_id: str) -> DecisionAggregation | None:
        stmt = select(DecisionAggregation).where(DecisionAggregation.decision_id == decision_id)
        return self._session.scalars(stmt).first()

    def list_by_symbol(
        self, bot_run_id: str, symbol: str, *, limit: int = 100
    ) -> list[DecisionAggregation]:
        stmt = (
            select(DecisionAggregation)
            .where(
                DecisionAggregation.bot_run_id == bot_run_id,
                DecisionAggregation.symbol == symbol,
            )
            .order_by(DecisionAggregation.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))


class RiskValidationRepository(BaseRepository[RiskValidation]):
    model = RiskValidation

    def get_by_aggregation_id(self, decision_aggregation_id: str) -> RiskValidation | None:
        stmt = select(RiskValidation).where(
            RiskValidation.decision_aggregation_id == decision_aggregation_id
        )
        return self._session.scalars(stmt).first()

    def list_by_result(
        self, bot_run_id: str, result: str, *, limit: int = 100
    ) -> list[RiskValidation]:
        """result: APPROVE | ADJUST_DOWN | BLOCK"""
        stmt = (
            select(RiskValidation)
            .where(
                RiskValidation.bot_run_id == bot_run_id,
                RiskValidation.result == result,
            )
            .order_by(RiskValidation.timestamp.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))
