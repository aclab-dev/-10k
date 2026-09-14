"""Fixtures para la suite de smoke tests (F17 [124]).

A diferencia de `tests/integration` (Postgres efímero vía testcontainers) y
`tests/chaos` (SQLite en memoria), esta suite habla contra un stack real ya
levantado con `docker compose up` — la separación de proceso worker/app es
justamente lo que se quiere verificar (el worker se entera de un cambio de
estado releyendo `bot_state` desde su propio proceso, no en memoria
compartida; ver `backend/api/routes_kill_switch.py`). Por eso no la levanta
el fixture: requiere un stack corriendo de antemano y se salta con un mensaje
claro si no lo encuentra, en vez de fallar.

Variables de entorno (todas opcionales, con default para el stack local
default de `docker-compose.yml` + `.env.example`):

    SMOKE_APP_BASE_URL       default http://localhost:8000
    SMOKE_DASHBOARD_USERNAME default admin (= DASHBOARD_USERNAME default)
    SMOKE_DASHBOARD_PASSWORD sin default — el hash en .env es de un solo
                             sentido, no hay forma de derivar la password en
                             claro. Sin esta var el test se salta.
    SMOKE_DATABASE_URL       default postgresql+psycopg2://bot:changeme_local_only@localhost:5434/cryptobot
                             (mismos defaults locales que docker-compose.yml)
"""

from __future__ import annotations

import os
from collections.abc import Generator
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import Engine, create_engine


@dataclass(frozen=True)
class SmokeConfig:
    app_base_url: str
    dashboard_username: str
    dashboard_password: str
    database_url: str


@pytest.fixture(scope="session")
def smoke_config() -> SmokeConfig:
    password = os.environ.get("SMOKE_DASHBOARD_PASSWORD")
    if not password:
        pytest.skip(
            "SMOKE_DASHBOARD_PASSWORD no está seteada — no hay forma de derivar la "
            "password en claro desde DASHBOARD_PASSWORD_HASH (hash de un solo sentido). "
            "Setear la misma password en claro usada al generar el hash local con "
            "scripts/hash_password.py."
        )
    return SmokeConfig(
        app_base_url=os.environ.get("SMOKE_APP_BASE_URL", "http://localhost:8000"),
        dashboard_username=os.environ.get("SMOKE_DASHBOARD_USERNAME", "admin"),
        dashboard_password=password,
        database_url=os.environ.get(
            "SMOKE_DATABASE_URL",
            "postgresql+psycopg2://bot:changeme_local_only@localhost:5434/cryptobot",
        ),
    )


@pytest.fixture(scope="session")
def smoke_http(smoke_config: SmokeConfig) -> Generator[httpx.Client, None, None]:
    with httpx.Client(base_url=smoke_config.app_base_url, timeout=10.0) as client:
        try:
            health = client.get("/health")
        except httpx.ConnectError:
            pytest.skip(
                f"No se pudo conectar a {smoke_config.app_base_url} — "
                "¿está el stack levantado? (`docker compose up -d`)"
            )
        if health.status_code != 200:
            pytest.skip(f"/health devolvió {health.status_code}, stack no está sano")
        yield client


@pytest.fixture(scope="session")
def smoke_db_engine(smoke_config: SmokeConfig) -> Generator[Engine, None, None]:
    engine = create_engine(smoke_config.database_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture
def smoke_token(smoke_http: httpx.Client, smoke_config: SmokeConfig) -> str:
    response = smoke_http.post(
        "/api/auth/login",
        json={
            "username": smoke_config.dashboard_username,
            "password": smoke_config.dashboard_password,
        },
    )
    if response.status_code != 200:
        pytest.skip(
            f"Login falló ({response.status_code}) — revisar "
            "SMOKE_DASHBOARD_USERNAME/SMOKE_DASHBOARD_PASSWORD contra el .env del stack."
        )
    token: str = response.json()["access_token"]
    return token
