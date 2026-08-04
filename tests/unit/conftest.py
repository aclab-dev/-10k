"""Fixtures compartidas para tests de endpoints REST (backend/api/routes_*.py).

Usan SQLite en memoria (misma estrategia que test_repositories.py) y un
TestClient de FastAPI con `get_db` sobreescrito para apuntar a esa sesión.

Auth (F15): los endpoints del dashboard exigen bearer token. El fixture `client`
hace un login real contra `/api/auth/login` y manda el token en cada request, así
que los tests de negocio no se enteran. Para verificar los 401 está `anon_client`,
que no manda ninguna credencial.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.main import app
from backend.auth.config import AuthCredentials, get_auth_credentials
from backend.auth.hashing import hash_password
from backend.storage.database import Base, get_db
from backend.storage.models import (
    AccountState,
    BotRun,
    BotState,
    Decision,
    RiskValidation,
    TokenUsage,
)

TEST_USERNAME = "test-user"
TEST_PASSWORD = "test-password-123"
TEST_SECRET_KEY = "test-secret-key-con-mas-de-32-caracteres"
TEST_TOKEN_TTL_SECONDS = 3600

# scrypt cuesta ~64 MB y ~100 ms por derivación: hasheamos la password de test
# una sola vez para todo el módulo en vez de una vez por test.
_TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)


def make_auth_credentials(*, enabled: bool = True) -> AuthCredentials:
    """Credenciales de test, sin tocar el entorno real ni config.yaml."""
    return AuthCredentials(
        enabled=enabled,
        username=TEST_USERNAME,
        password_hash=_TEST_PASSWORD_HASH,
        secret_key=TEST_SECRET_KEY,
        token_ttl_seconds=TEST_TOKEN_TTL_SECONDS,
    )


@pytest.fixture
def engine():
    # StaticPool: TestClient ejecuta las dependencias sync en un worker thread
    # distinto al del test; sqlite:///:memory: sin StaticPool le daría a ese
    # thread una conexión (y por lo tanto una DB) nueva y vacía.
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine) -> Generator[Session, None, None]:
    with Session(engine) as s:
        yield s


@pytest.fixture
def anon_client(session: Session) -> Generator[TestClient, None, None]:
    """Cliente sin credenciales, con la auth habilitada. Para verificar los 401."""

    def _get_db() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_auth_credentials] = make_auth_credentials
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client(anon_client: TestClient) -> TestClient:
    """Cliente autenticado vía login real — el token viaja en cada request."""
    response = anon_client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    anon_client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    return anon_client


def uid() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(UTC)


def make_bot_run(session: Session, *, status: str = "RUNNING") -> BotRun:
    run = BotRun(
        environment="PAPER",
        app_version="0.1.0",
        config_snapshot={"test": True},
        status=status,
    )
    session.add(run)
    session.flush()
    return run


def make_bot_state(
    session: Session, bot_run: BotRun, *, state: str = "IDLE", previous_state: str | None = None
) -> BotState:
    bot_state = BotState(
        bot_run_id=bot_run.id,
        state=state,
        previous_state=previous_state,
        reason="test",
    )
    session.add(bot_state)
    session.flush()
    return bot_state


def make_account_state(
    session: Session,
    bot_run: BotRun,
    *,
    timestamp: datetime | None = None,
    balance_usdt: Decimal = Decimal("1000"),
    equity_usdt: Decimal = Decimal("1000"),
    drawdown_percent: float = 0.0,
) -> AccountState:
    account_state = AccountState(
        bot_run_id=bot_run.id,
        timestamp=timestamp or now(),
        balance_usdt=balance_usdt,
        equity_usdt=equity_usdt,
        margin_used_usdt=Decimal("0"),
        unrealized_pnl=Decimal("0"),
        realized_pnl_session=Decimal("0"),
        drawdown_percent=drawdown_percent,
        exposure_percent=0.0,
        environment="PAPER",
    )
    session.add(account_state)
    session.flush()
    return account_state


def make_decision(
    session: Session,
    bot_run: BotRun,
    *,
    symbol: str = "BTCUSDT",
    action: str = "OPEN",
    timestamp: datetime | None = None,
) -> Decision:
    decision = Decision(
        bot_run_id=bot_run.id,
        symbol=symbol,
        timestamp=timestamp or now(),
        action=action,
        direction="LONG" if action == "OPEN" else None,
        confidence=0.8,
    )
    session.add(decision)
    session.flush()
    return decision


def make_risk_validation(
    session: Session,
    bot_run: BotRun,
    *,
    symbol: str = "BTCUSDT",
    result: str = "BLOCK",
    timestamp: datetime | None = None,
) -> RiskValidation:
    risk_validation = RiskValidation(
        bot_run_id=bot_run.id,
        symbol=symbol,
        timestamp=timestamp or now(),
        result=result,
        reasons={"rule": "max_margin_exceeded"} if result == "BLOCK" else None,
    )
    session.add(risk_validation)
    session.flush()
    return risk_validation


def make_token_usage(
    session: Session,
    bot_run: BotRun,
    *,
    model: str = "gpt-5.5",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    timestamp: datetime | None = None,
) -> TokenUsage:
    token_usage = TokenUsage(
        bot_run_id=bot_run.id,
        timestamp=timestamp or now(),
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    session.add(token_usage)
    session.flush()
    return token_usage
