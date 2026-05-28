"""Tests para audit_model_request / audit_model_response y GPTClient con auditoría."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.core.config import FailurePolicyConfig
from backend.decision_engine.gpt_client import (
    GPTClient,
    GPTClientConfig,
    GPTRequest,
    RequestPurpose,
    _compute_request_hash,
)
from backend.decision_engine.schemas import ModelDecision
from backend.storage.audit import audit_model_request, audit_model_response
from backend.storage.database import Base
from backend.storage.models import BotRun, ModelRequest, ModelResponse

# ---------------------------------------------------------------------------
# DB fixtures (SQLite in-memory, mismo patrón que test_audit.py)
# ---------------------------------------------------------------------------

_TEST_API_KEY = "sk-test-key-not-real"

_POLICY = FailurePolicyConfig(
    gpt_failure_blocks_new_entries=True,
    token_budget_failure_blocks_new_entries=True,
    deterministic_position_management_without_gpt=True,
    exits_do_not_require_gpt_response=True,
    every_position_requires_confirmed_sl_tp=True,
)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s
        s.rollback()


@pytest.fixture
def bot_run(session: Session) -> BotRun:
    run = BotRun(
        id=str(uuid.uuid4()),
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"mode": "PAPER"},
        status="RUNNING",
    )
    session.add(run)
    session.flush()
    return run


def _make_gpt_client(max_retries: int = 0) -> GPTClient:
    config = GPTClientConfig(max_retries=max_retries, base_delay_seconds=0.0)
    return GPTClient(config=config, failure_policy=_POLICY, api_key=_TEST_API_KEY)


def _make_req() -> GPTRequest:
    return GPTRequest(
        system_prompt="You are a trading evaluator.",
        user_prompt="Evaluate this context.",
        prompt_version="1.0",
    )


def _valid_decision_dict() -> dict[str, Any]:
    return {
        "decision_id": str(uuid.uuid4()),
        "challenge_mode": "AUTONOMOUS_FUTURES_GPT55_QUANT_CONTROLLED_RISK",
        "schema_version": "1.0",
        "environment": "PAPER",
        "timestamp_utc": "2026-05-25T10:00:00Z",
        "decision": "NO_OPERAR",
        "symbol": "BTCUSDT",
        "market": "USDT_M_FUTURES",
        "margin_type": "ISOLATED",
        "position_mode": "ONE_WAY",
        "entry_type": "NO_ENTRY",
        "entry_price": 0.0,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "invalidation_price": 0.0,
        "leverage": 1,
        "margin_usdt": 0.0,
        "estimated_notional_usdt": 0.0,
        "estimated_entry_fee_usdt": 0.0,
        "estimated_exit_fee_usdt": 0.0,
        "estimated_slippage_usdt": 0.0,
        "estimated_funding_usdt": 0.0,
        "net_risk_reward": 0.0,
        "estimated_max_loss_usdt": 0.0,
        "liquidation_distance_percent_estimated": 0.0,
        "confidence": 0.0,
        "market_regime": "UNCLEAR",
        "setup_name": "no_setup",
        "timeframes_used": ["5m", "15m", "1h", "4h"],
        "quant_signals": {
            "momentum": "BULLISH",
            "mean_reversion": "LONG_BIAS",
            "breakout_detection": "CONFIRMED",
            "funding_analysis": "SUPPORTS_TRADE",
            "open_interest_analysis": "RISING_WITH_PRICE",
            "order_flow_imbalance": "BUY_PRESSURE",
            "liquidity_sweep": "NONE",
        },
        "decision_aggregator": {
            "quant_score": 0.75,
            "gpt_context_score": 0.70,
            "risk_quality_score": 0.80,
            "final_trade_quality_score": 0.75,
        },
        "news_context": {"used": False, "impact": "NEUTRAL", "summary": "No news."},
        "position_management_plan": {
            "use_trailing_stop": False,
            "move_to_break_even": False,
            "partial_close_plan": "none",
            "max_time_in_trade_minutes": 0,
        },
        "decision_rationale_summary": "No hay edge.",
        "risk_notes": [],
        "execute": False,
    }


def _openai_response(content: str, finish_reason: str = "stop") -> MagicMock:
    full_body = {
        "choices": [
            {"message": {"content": content, "role": "assistant"}, "finish_reason": finish_reason}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 200
    mock_resp.text = json.dumps(full_body)  # wire-original, igual que lo devuelve OpenAI
    mock_resp.headers = MagicMock()
    mock_resp.json.return_value = full_body
    return mock_resp


# ---------------------------------------------------------------------------
# Tests: audit_model_request
# ---------------------------------------------------------------------------


class TestAuditModelRequest:
    def test_persists_record(self, session: Session, bot_run: BotRun) -> None:
        ctx = {"system_prompt": "sys", "user_prompt": "usr", "prompt_version": "1.0"}
        req_hash = _compute_request_hash("gpt-4o", "sys", "usr")

        record = audit_model_request(
            session,
            bot_run_id=bot_run.id,
            symbol="BTCUSDT",
            model="gpt-4o",
            context=ctx,
            request_hash=req_hash,
        )

        assert record.id is not None
        fetched = session.get(ModelRequest, record.id)
        assert fetched is not None
        assert fetched.bot_run_id == bot_run.id
        assert fetched.symbol == "BTCUSDT"
        assert fetched.model == "gpt-4o"
        assert fetched.request_hash == req_hash

    def test_optional_fields_default_to_none(self, session: Session, bot_run: BotRun) -> None:
        ctx = {"system_prompt": "s", "user_prompt": "u", "prompt_version": "1.0"}
        record = audit_model_request(
            session,
            bot_run_id=bot_run.id,
            symbol="ETHUSDT",
            model="gpt-4o",
            context=ctx,
            request_hash=_compute_request_hash("gpt-4o", "s", "u"),
        )
        assert record.feature_package_id is None

    def test_timestamp_is_set(self, session: Session, bot_run: BotRun) -> None:
        ctx = {"system_prompt": "s2", "user_prompt": "u2", "prompt_version": "1.0"}
        record = audit_model_request(
            session,
            bot_run_id=bot_run.id,
            symbol="BTCUSDT",
            model="gpt-4o",
            context=ctx,
            request_hash=_compute_request_hash("gpt-4o", "s2", "u2"),
        )
        assert record.timestamp is not None


# ---------------------------------------------------------------------------
# Tests: audit_model_response
# ---------------------------------------------------------------------------


class TestAuditModelResponse:
    def _create_model_request(self, session: Session, bot_run: BotRun) -> ModelRequest:
        ctx = {"system_prompt": "sp", "user_prompt": "up", "prompt_version": "1.0"}
        return audit_model_request(
            session,
            bot_run_id=bot_run.id,
            symbol="BTCUSDT",
            model="gpt-4o",
            context=ctx,
            request_hash=_compute_request_hash("gpt-4o", "sp", "up"),
        )

    def test_persists_record(self, session: Session, bot_run: BotRun) -> None:
        req = self._create_model_request(session, bot_run)
        raw = json.dumps({"choices": [], "usage": {}})

        resp = audit_model_response(
            session,
            model_request_id=req.id,
            model="gpt-4o",
            raw_response=raw,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            finish_reason="stop",
            is_valid_schema=True,
        )

        assert resp.id is not None
        fetched = session.get(ModelResponse, resp.id)
        assert fetched is not None
        assert fetched.model_request_id == req.id
        assert fetched.raw_response == raw
        assert fetched.prompt_tokens == 100
        assert fetched.completion_tokens == 50
        assert fetched.total_tokens == 150
        assert fetched.finish_reason == "stop"
        assert fetched.is_valid_schema is True

    def test_normalized_response_stored(self, session: Session, bot_run: BotRun) -> None:
        req = self._create_model_request(session, bot_run)
        normalized = {"action": "NO_OPERAR", "confidence": 0.0}

        resp = audit_model_response(
            session,
            model_request_id=req.id,
            model="gpt-4o",
            raw_response="{}",
            normalized_response=normalized,
            is_valid_schema=True,
        )

        fetched = session.get(ModelResponse, resp.id)
        assert fetched is not None
        assert fetched.normalized_response == normalized

    def test_invalid_schema_flag(self, session: Session, bot_run: BotRun) -> None:
        req = self._create_model_request(session, bot_run)

        resp = audit_model_response(
            session,
            model_request_id=req.id,
            model="gpt-4o",
            raw_response='{"bad": "data"}',
            is_valid_schema=False,
        )

        fetched = session.get(ModelResponse, resp.id)
        assert fetched is not None
        assert fetched.is_valid_schema is False
        assert fetched.normalized_response is None


# ---------------------------------------------------------------------------
# Tests: _compute_request_hash
# ---------------------------------------------------------------------------


class TestComputeRequestHash:
    def test_deterministic(self) -> None:
        h1 = _compute_request_hash("gpt-4o", "system", "user")
        h2 = _compute_request_hash("gpt-4o", "system", "user")
        assert h1 == h2

    def test_different_model_different_hash(self) -> None:
        h1 = _compute_request_hash("gpt-4o", "system", "user")
        h2 = _compute_request_hash("gpt-4-turbo", "system", "user")
        assert h1 != h2

    def test_different_prompt_different_hash(self) -> None:
        h1 = _compute_request_hash("gpt-4o", "system A", "user")
        h2 = _compute_request_hash("gpt-4o", "system B", "user")
        assert h1 != h2

    def test_returns_sha256_hex_length(self) -> None:
        h = _compute_request_hash("gpt-4o", "system", "user")
        assert len(h) == 64


# ---------------------------------------------------------------------------
# Tests: GPTClient.request() con auditoría
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGPTClientWithAudit:
    async def test_persists_request_and_response_on_success(
        self, session: Session, bot_run: BotRun
    ) -> None:
        client = _make_gpt_client()
        req = _make_req()
        content = json.dumps(_valid_decision_dict())
        mock_resp = _openai_response(content)

        from unittest.mock import patch

        from sqlalchemy import select

        with patch.object(
            client._http_client, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await client.request(
                req,
                RequestPurpose.NEW_ENTRY,
                session=session,
                bot_run_id=bot_run.id,
                symbol="BTCUSDT",
            )

        assert isinstance(result, ModelDecision)

        req_records = list(
            session.scalars(select(ModelRequest).where(ModelRequest.bot_run_id == bot_run.id))
        )
        assert len(req_records) == 1
        mr = req_records[0]
        assert mr.symbol == "BTCUSDT"
        assert mr.model == "gpt-4o"
        assert mr.request_hash is not None
        assert "system_prompt" in mr.context

        # Verificar ModelResponse persistido
        resp_records = list(
            session.scalars(select(ModelResponse).where(ModelResponse.model_request_id == mr.id))
        )
        assert len(resp_records) == 1
        resp = resp_records[0]
        assert resp.is_valid_schema is True
        assert resp.prompt_tokens == 100
        assert resp.completion_tokens == 50
        assert resp.total_tokens == 150
        assert resp.finish_reason == "stop"
        assert resp.normalized_response is not None

    async def test_no_db_calls_without_session(self, bot_run: BotRun) -> None:
        client = _make_gpt_client()
        req = _make_req()
        content = json.dumps(_valid_decision_dict())
        mock_resp = _openai_response(content)

        from unittest.mock import patch

        with (
            patch.object(
                client._http_client, "post", new_callable=AsyncMock, return_value=mock_resp
            ),
            patch("backend.decision_engine.gpt_client.audit_model_request") as mock_audit_req,
            patch("backend.decision_engine.gpt_client.audit_model_response") as mock_audit_resp,
        ):
            result = await client.request(req, RequestPurpose.NEW_ENTRY)

        assert isinstance(result, ModelDecision)
        mock_audit_req.assert_not_called()
        mock_audit_resp.assert_not_called()

    async def test_persists_request_with_invalid_schema_response(
        self, session: Session, bot_run: BotRun
    ) -> None:
        """Respuesta fuera de schema → siempre bloquea (raise) + audit persistido.

        Desde la integración del InvalidResponseHandler, las respuestas inválidas
        siempre levantan GPTResponseValidationError independientemente de la failure
        policy. La capa de auditoría igual debe persistir la respuesta con
        is_valid_schema=False antes de que el error se propague.
        """
        from unittest.mock import patch

        from sqlalchemy import select

        from backend.decision_engine.gpt_client import GPTResponseValidationError

        config = GPTClientConfig(max_retries=0, base_delay_seconds=0.0)
        client = GPTClient(config=config, failure_policy=_POLICY, api_key=_TEST_API_KEY)

        req = _make_req()
        bad_content = json.dumps({"not": "a_valid_decision"})
        mock_resp = _openai_response(bad_content)

        with (
            patch.object(
                client._http_client, "post", new_callable=AsyncMock, return_value=mock_resp
            ),
            pytest.raises(GPTResponseValidationError),
        ):
            await client.request(
                req,
                RequestPurpose.NEW_ENTRY,
                session=session,
                bot_run_id=bot_run.id,
                symbol="ETHUSDT",
            )

        resp_records = list(
            session.scalars(
                select(ModelResponse)
                .join(ModelRequest, ModelResponse.model_request_id == ModelRequest.id)
                .where(
                    ModelRequest.bot_run_id == bot_run.id,
                    ModelRequest.symbol == "ETHUSDT",
                )
            )
        )
        assert len(resp_records) == 1
        assert resp_records[0].is_valid_schema is False
        assert resp_records[0].normalized_response is None

    async def test_returns_decision_when_audit_response_fails(
        self, session: Session, bot_run: BotRun
    ) -> None:
        """audit_model_response falla → ModelDecision se retorna igual (trading > audit)."""
        from unittest.mock import patch

        client = _make_gpt_client()
        req = _make_req()
        content = json.dumps(_valid_decision_dict())
        mock_resp = _openai_response(content)

        with (
            patch.object(
                client._http_client, "post", new_callable=AsyncMock, return_value=mock_resp
            ),
            patch(
                "backend.decision_engine.gpt_client.audit_model_response",
                side_effect=Exception("DB exploded"),
            ),
        ):
            result = await client.request(
                req,
                RequestPurpose.NEW_ENTRY,
                session=session,
                bot_run_id=bot_run.id,
                symbol="BNBUSDT",
            )

        assert isinstance(result, ModelDecision)

    async def test_model_request_persisted_when_audit_response_fails(
        self, session: Session, bot_run: BotRun
    ) -> None:
        """ModelRequest queda persistido aunque audit_model_response explote."""
        from unittest.mock import patch

        from sqlalchemy import select

        client = _make_gpt_client()
        req = _make_req()
        content = json.dumps(_valid_decision_dict())
        mock_resp = _openai_response(content)

        with (
            patch.object(
                client._http_client, "post", new_callable=AsyncMock, return_value=mock_resp
            ),
            patch(
                "backend.decision_engine.gpt_client.audit_model_response",
                side_effect=Exception("DB exploded"),
            ),
        ):
            await client.request(
                req,
                RequestPurpose.NEW_ENTRY,
                session=session,
                bot_run_id=bot_run.id,
                symbol="SOLUSDT",
            )

        req_records = list(
            session.scalars(
                select(ModelRequest).where(
                    ModelRequest.bot_run_id == bot_run.id,
                    ModelRequest.symbol == "SOLUSDT",
                )
            )
        )
        assert len(req_records) == 1
        assert req_records[0].request_hash is not None
