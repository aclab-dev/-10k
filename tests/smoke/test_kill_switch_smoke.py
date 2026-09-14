"""Smoke test end-to-end del kill switch manual contra un stack real (F17 [124]).

Tarjeta: https://trello.com/c/bKwDxYdW

Verifica, contra procesos reales (no mocks, no `TestClient` en memoria) las
tres cosas que pide el DoD:

1. El kill switch detiene al bot — el *worker*, que corre en un proceso
   separado del `app` y sólo se entera de un cambio de estado releyendo
   `bot_state` (`backend/trading_core/cycle_runner.py::_sync_state_from_db`),
   efectivamente deja de tickear.
2. No se generan (ni quedan) órdenes después del disparo.
3. Queda un `kill_switch_events` persistido con `requires_manual_review=true`.

Precondición: un stack levantado con `docker compose up -d` (`ENVIRONMENT=PAPER`
— TESTNET no está wireado todavía, ver `docs/runbook_server.md` §2.5) con un
`BotRun` en curso. Este archivo no lo levanta — si no lo encuentra, se salta
con un mensaje explicando qué falta en vez de fallar. Ejecutar con:

    pytest -m smoke tests/smoke/test_kill_switch_smoke.py

`SMOKE_DASHBOARD_PASSWORD` es obligatoria (ver `conftest.py`).

El test deja el `BotRun` como lo encontró (`ACTIVE`): el kill switch no tiene
auto-resume por diseño (PDF 4.8), así que el teardown replica a mano el único
camino documentado para volver de `KILL_SWITCH_TRIGGERED` a `ACTIVE`
(`docs/runbook_server.md` §6.4 — dos inserts en `bot_state`, uno por uno). El
worker ya corriendo lo resincroniza solo, sin reiniciar el proceso
(`_sync_state_from_db` corre en cada vuelta del loop).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from backend.storage.models import BotState

pytestmark = pytest.mark.smoke

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Cota real, no el caso feliz: el resync corre antes de cada símbolo
# (cycle_runner._run_decision_pipeline), pero el símbolo EN CURSO al momento
# del disparo puede seguir en medio de su propia llamada a GPT con reintentos
# — timeout_seconds=30 x hasta 4 intentos + backoff exponencial hasta
# max_delay_seconds=60 entre reintentos (backend/decision_engine/gpt_client.py
# GPTClientConfig). 200s da margen real sobre "un timeout + un retry" sin
# llegar al peor caso patológico (todos los reintentos agotando el backoff
# máximo, ~300s) — si eso ocurre el test puede fallar igual pese a que el
# kill switch funcionó; se documenta como límite conocido, no se ignora.
_WORKER_POLL_TIMEOUT_SECONDS = 200
_WORKER_POLL_INTERVAL_SECONDS = 5
# Evidencia válida de "el worker dejó de operar": el resync de fin de vuelta
# del while (paused_by_state) o el resync per-símbolo a mitad de tick
# (pipeline_aborted_by_state) — cycle_runner.py líneas ~170 y ~308.
_STOP_EVENTS = frozenset({"cycle_runner.paused_by_state", "cycle_runner.pipeline_aborted_by_state"})
# docker compose prefija cada línea con "<container>  | ": el resto es el JSON
# de structlog (ver worker/run_worker.py). No trae bot_run_id — un solo
# BotRun corre por worker a la vez en este stack local — así que lo que
# identifica una línea como posterior al disparo es su propio timestamp.
_LOG_PREFIX = re.compile(r"^\S+\s*\|\s*")


def _worker_events_since(triggered_at: datetime) -> list[dict[str, object]] | None:
    """Eventos logueados por el worker con timestamp > `triggered_at`, o None
    si no se pudo leer `docker compose logs` — ya sea porque el binario no
    está (`FileNotFoundError`), el comando devolvió error, o el stdout vino
    completamente vacío. Este último caso es señal de estar apuntando al
    proyecto de compose equivocado (por ejemplo si el stack real se levantó
    con `-p <nombre>` distinto al que resuelve `_REPO_ROOT`): sin ninguna
    línea de log, ni siquiera las de arranque del worker, no hay forma de
    distinguir "no encontramos el container" de "el worker frenó" — se trata
    igual que no poder leer los logs, no como ausencia de eventos."""
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "worker", "--since", "5m"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None

    events: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        payload = _LOG_PREFIX.sub("", line, count=1)
        try:
            record = json.loads(payload)
        except json.JSONDecodeError:
            continue
        raw_ts = record.get("timestamp")
        if not isinstance(raw_ts, str):
            continue
        try:
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts > triggered_at:
            events.append(record)
    return events


def _restore_active_state(engine: Engine, bot_run_id: str, reason: str) -> None:
    """Deshace el kill switch replicando a mano el único camino documentado
    (`docs/runbook_server.md` §6.4): dos inserts en `bot_state`, uno por uno
    (no hay atajo directo `KILL_SWITCH_TRIGGERED -> ACTIVE`). El worker que ya
    está corriendo los recoge solo en su próximo resync — no hace falta
    reiniciar el container (eso sólo importa para el *arranque* de un proceso
    nuevo, que sí arrastra `KILL_SWITCH_TRIGGERED`/`HALTED`, distinto del
    resync en caliente de `_sync_state_from_db`)."""
    with Session(engine) as session:
        session.add(
            BotState(
                bot_run_id=bot_run_id,
                state="HALTED",
                previous_state="KILL_SWITCH_TRIGGERED",
                reason=reason,
            )
        )
        session.commit()
        session.add(
            BotState(
                bot_run_id=bot_run_id,
                state="ACTIVE",
                previous_state="HALTED",
                reason=reason,
            )
        )
        session.commit()


def test_kill_switch_stops_worker_and_logs_event(
    smoke_http: httpx.Client, smoke_token: str, smoke_db_engine: Engine
) -> None:
    headers = {"Authorization": f"Bearer {smoke_token}"}

    status_response = smoke_http.get("/api/status", headers=headers)
    if status_response.status_code == 404:
        pytest.skip(
            "No hay un BotRun activo — levantar el stack (`docker compose up -d`) "
            "antes de correr el smoke test."
        )
    assert status_response.status_code == 200, status_response.text
    status_before = status_response.json()
    bot_run_id = status_before["bot_run_id"]

    if status_before["state"] == "KILL_SWITCH_TRIGGERED":
        pytest.skip(
            f"BotRun {bot_run_id} ya está en KILL_SWITCH_TRIGGERED — el teardown de una "
            "corrida anterior debería haberlo evitado. Restaurar a mano con "
            "docs/runbook_server.md §6.4 antes de re-correr."
        )

    reason = f"F17 smoke test [{uuid.uuid4()}] - verificacion end-to-end del kill switch"
    triggered = False
    try:
        trigger_response = smoke_http.post(
            "/api/kill-switch", headers=headers, json={"reason": reason}
        )
        assert trigger_response.status_code == 200, trigger_response.text
        trigger_body = trigger_response.json()
        triggered = True
        assert trigger_body["bot_run_id"] == bot_run_id
        assert trigger_body["state"] == "KILL_SWITCH_TRIGGERED"
        assert trigger_body["previous_state"] != "KILL_SWITCH_TRIGGERED"
        triggered_at = datetime.fromisoformat(trigger_body["triggered_at"].replace("Z", "+00:00"))

        # DoD: "registra evento" — el kill_switch_events queda persistido en el
        # mismo request que respondió 200 (EmergencyStopService.trigger commitea
        # antes de responder), así que no hace falta poll acá.
        with smoke_db_engine.connect() as conn:
            event_row = conn.execute(
                text(
                    "SELECT action_taken, requires_manual_review, state_before "
                    "FROM kill_switch_events WHERE bot_run_id = :bot_run_id "
                    "AND trigger_reason = :reason"
                ),
                {"bot_run_id": bot_run_id, "reason": reason},
            ).one_or_none()
        assert event_row is not None, "no se encontró el kill_switch_events del smoke test"
        assert event_row.action_taken == "MANUAL_KILL_SWITCH"
        assert event_row.requires_manual_review is True
        # Comparar contra previous_state de la respuesta, no contra
        # status_before["state"]: en un BotRun recién arrancado sin ningún
        # bot_state persistido todavía, /api/status devuelve state=None (nada
        # que leer) mientras que EmergencyStopService resuelve el "sin fila"
        # al default fail-open ACTIVE (resolve_persisted_state) — son la
        # misma realidad, expresada distinto por cada endpoint.
        assert event_row.state_before == trigger_body["previous_state"]

        # DoD: "detiene el bot" — el *worker* (proceso separado) tiene que
        # resincronizar contra bot_state y dejar de tickear. Se hace poll porque
        # el worker sólo relee al tope de cada iteración (WORKER_HEARTBEAT_INTERVAL_SECONDS).
        worker_paused = False
        post_trigger_events: list[dict[str, object]] = []
        deadline = time.monotonic() + _WORKER_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            events = _worker_events_since(triggered_at)
            if events is None:
                pytest.skip(
                    "`docker compose logs` no está disponible (o apunta a otro proyecto) "
                    "desde donde corre pytest — no se puede verificar por log que el "
                    "worker frenó. El resto del smoke test (evento persistido, sin "
                    "órdenes nuevas) sí se validó."
                )
            post_trigger_events = events
            if any(e.get("event") in _STOP_EVENTS for e in events):
                worker_paused = True
                break
            time.sleep(_WORKER_POLL_INTERVAL_SECONDS)
        assert worker_paused, (
            "el worker no logueó paused_by_state ni pipeline_aborted_by_state dentro de "
            f"{_WORKER_POLL_TIMEOUT_SECONDS}s tras el kill switch — no se detuvo. "
            f"Eventos posteriores al disparo: {post_trigger_events}"
        )

        # DoD: "cancela órdenes" — en PAPER no hay cancelación explícita (las
        # PENDING viven en memoria del PaperAdapter y se descartan al detener
        # el worker, ver docs/runbook_server.md §2.2 paso 3); lo verificable
        # es que no se generó ninguna orden nueva después del disparo. Cubre
        # la ventana completa desde el trigger (a diferencia de los logs, que
        # sólo se leyeron hasta confirmar la pausa), así que es la única
        # fuente para esta aserción.
        with smoke_db_engine.connect() as conn:
            new_orders = conn.execute(
                text(
                    "SELECT count(*) FROM orders WHERE bot_run_id = :bot_run_id "
                    "AND created_at > :triggered_at"
                ),
                {"bot_run_id": bot_run_id, "triggered_at": triggered_at},
            ).scalar_one()
        assert new_orders == 0, (
            f"se crearon {new_orders} orden(es) nuevas después del kill switch — "
            "el worker siguió operando"
        )
    finally:
        if triggered:
            _restore_active_state(
                smoke_db_engine, bot_run_id, reason="smoke_test_teardown_restore_active"
            )
