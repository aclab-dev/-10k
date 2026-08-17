"""Test de integración: DASHBOARD_PASSWORD_HASH sobrevive la interpolación de
Docker Compose (tarjeta [156], F15).

Reproduce el bug real: Docker Compose interpola `.env` antes de pasarlo a los
containers, y sin escapar los `$` del hash scrypt cada segmento posterior se
trata como una variable no definida y se reemplaza por string vacío. Este test
levanta el servicio `app` del docker-compose.yml real del repo (sin sus
dependencias) con un `.env` generado por scripts/hash_password.py (ya
escapado) y lee la env var *dentro del container* — no alcanza con mirar
`docker compose config`, que re-escapa `$` en su propio output para poder
alimentarse a sí mismo, así que nunca refleja el valor real que ve la app.

Requiere el binario `docker` (con soporte `compose`) disponible en el host y
la imagen del servicio `app` ya buildeada (`docker compose build app`).

Ejecutar con: pytest -m integration -k docker_compose
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend.auth.hashing import hash_password
from scripts.hash_password import escape_dollar_for_dotenv

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FAST_HASH_PARAMS = {"n": 2**4, "r": 1, "p": 1}


@pytest.fixture(scope="module", autouse=True)
def _require_docker_compose() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker no está disponible — test de integración omitido.")
    probe = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("docker compose no está disponible — test de integración omitido.")


def _resolved_dashboard_password_hash(env_path: Path) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_path),
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "printenv",
            "app",
            "DASHBOARD_PASSWORD_HASH",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    # Si la imagen no está prebuildeada (o quedó desactualizada), `docker compose run`
    # la construye al vuelo y el progreso de BuildKit se intercala en stdout antes del
    # valor real de printenv. Nos quedamos con la última línea no vacía para no
    # depender de que el caller haya corrido `docker compose build app` antes.
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def test_escaped_hash_survives_compose_interpolation(tmp_path: Path) -> None:
    original_hash = hash_password("una-password-de-prueba", **_FAST_HASH_PARAMS)
    assert original_hash.count("$") == 5  # scrypt$n$r$p$salt$hash

    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=postgresql://bot:x@localhost:5434/cryptobot\n"
        f"DASHBOARD_PASSWORD_HASH={escape_dollar_for_dotenv(original_hash)}\n"
    )

    resolved = _resolved_dashboard_password_hash(env_path)

    assert resolved == original_hash


def test_unescaped_hash_gets_corrupted_by_compose_interpolation(tmp_path: Path) -> None:
    """Confirma que el bug es real: sin escapar, Compose sí corrompe el hash."""
    original_hash = hash_password("otra-password-de-prueba", **_FAST_HASH_PARAMS)

    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=postgresql://bot:x@localhost:5434/cryptobot\n"
        f"DASHBOARD_PASSWORD_HASH={original_hash}\n"
    )

    resolved = _resolved_dashboard_password_hash(env_path)

    assert resolved != original_hash
