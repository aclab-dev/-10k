# Plan de apagado

Secuencia exacta para apagar `-10k` de forma segura, en emergencia y de forma
planeada. Todo lo descrito corresponde al comportamiento real del código en
`develop` a la fecha de este documento — no son recomendaciones genéricas.

Alcance: entorno `PAPER` en un solo host con Docker Compose
(`docker-compose.yml`). BingX real (`TESTNET`/`LIVE`) **no está soportado** — el
`Orchestrator` falla con `NotImplementedError` en cualquier `environment` que no
sea `PAPER` (`backend/trading_core/orchestrator.py::_prepare_paper_context`). La
§6 marca qué partes de este plan cambian cuando LIVE entre.

Complementa `docs/runbook_server.md` (arranque, recuperación tras crash,
backups, incidentes). Este documento cubre solo el apagado y el orden en que se
hacen las cosas.

---

## 1. Cuándo usar cada camino

| Situación | Camino | Interrumpe el ciclo en curso |
|---|---|---|
| Emergencia (comportamiento anómalo, pérdida fuera de lo esperado, incidente en el exchange o en OpenAI) | §2 — apagado de emergencia | Sí: kill switch primero, procesos después |
| Apagado planeado (deploy, mantenimiento del host, parada ordenada) | §4 — apagado planeado | No: espera a que termine el tick actual |

La diferencia central: el **apagado de emergencia dispara el kill switch antes
de tocar los procesos**, para que el bot deje de abrir posiciones aunque un tick
largo siga corriendo. El apagado planeado no necesita el kill switch — un
`SIGTERM` con timeout generoso alcanza.

---

## 2. Apagado de emergencia

Orden: **kill switch → verificar que el worker frenó → cancelar órdenes → cerrar
posiciones → detener procesos**. Los pasos 3 y 4 hoy no tienen tooling de
operador (ver §2.3, §2.4 y §6) — en `PAPER` son casi siempre innecesarios
porque el estado vive en memoria del worker y se descarta al detenerlo; se
documentan igual porque son parte del DoD de cara a LIVE y porque las filas de
DB sí sobreviven.

### 2.0 Antes de empezar

1. Obtener un token del dashboard:

   ```bash
   curl -sX POST localhost:8000/api/auth/login \
     -H "Content-Type: application/json" \
     -d '{"username":"<DASHBOARD_USERNAME>","password":"<contraseña>"}'
   ```

   Devuelve `{"access_token":"...","token_type":"bearer","expires_at":"..."}`
   (`backend/api/routes_auth.py::LoginResponse`). Exportarlo:

   ```bash
   TOKEN="<access_token>"
   ```

2. Anotar el estado actual, para saber contra qué se compara después:

   ```bash
   curl -s localhost:8000/api/status -H "Authorization: Bearer $TOKEN" \
     | python -m json.tool
   ```

   Campos relevantes (`backend/api/routes_status.py::BotStatusOut`):
   `run_status` (`RUNNING`/`STOPPED`/`CRASHED`), `state`
   (`ACTIVE`/`SAFE_MODE`/`HALTED`/`KILL_SWITCH_TRIGGERED`/`MANUAL_PAUSED`),
   `state_reason`, `account.unrealized_pnl`, `account.margin_used_usdt`,
   `account.exposure_percent`. **`/api/status` no expone posiciones ni órdenes
   individuales** — para eso, ver §2.3 y §2.4.

### 2.1 Disparar el kill switch

```bash
curl -sX POST localhost:8000/api/kill-switch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"<motivo concreto de la emergencia>"}'
```

Qué hace (`backend/api/routes_kill_switch.py` →
`backend/trading_core/emergency_stop.py::EmergencyStopService.trigger`):

- Toma lock `FOR UPDATE` sobre la fila del `BotRun` activo.
- Re-lee el estado persistido y valida la transición contra la state machine
  (`backend/trading_core/bot_state_machine.py`). `ACTIVE`, `SAFE_MODE` y
  `HALTED` pueden ir a `KILL_SWITCH_TRIGGERED`; desde
  `KILL_SWITCH_TRIGGERED` o `MANUAL_PAUSED` la llamada devuelve **409** y no
  hace nada.
- Persiste atómicamente una fila nueva en `bot_state`
  (`state=KILL_SWITCH_TRIGGERED`) y un `kill_switch_events` con
  `action_taken="MANUAL_KILL_SWITCH"` y `requires_manual_review=true`, y
  commitea.

Respuesta esperada: `200` con
`{"bot_run_id","state":"KILL_SWITCH_TRIGGERED","previous_state","reason","triggered_at"}`.

Códigos de error:

- **409** — el `BotRun` no está `RUNNING`, o la transición no está permitida
  desde el estado actual (mensaje indica cuál). Si ya estaba en
  `KILL_SWITCH_TRIGGERED`, no hay nada que hacer en este paso: seguir a §2.2.
- **500** — el último `bot_state.state` en DB está corrupto (fuera del enum).
  Escalar: hay un problema de datos, no de operación. El worker en este caso
  sigue con su último estado en memoria (fail-open,
  `cycle_runner._sync_state_from_db`).

### 2.2 Verificar que el worker dejó de operar

El worker corre en un proceso separado (`cryptobot-worker`) con su propia
`BotStateMachine` en memoria. **No se entera del kill switch al instante**:
relee `bot_state` al tope de cada iteración del loop y antes de cada símbolo del
pipeline de decisión (`backend/trading_core/cycle_runner.py::_sync_state_from_db`,
llamado desde `run()` y desde `_run_decision_pipeline`).

Latencia real hasta que frena:

- Entre ticks: hasta un `WORKER_HEARTBEAT_INTERVAL_SECONDS` (10s por defecto).
- Dentro de un tick: un símbolo que ya está a mitad de su llamada a GPT
  (timeout 30s por intento, hasta 4 intentos con backoff —
  `backend/decision_engine/gpt_client.py`) termina de procesarse antes de
  frenar. Los símbolos siguientes del mismo tick ya no abren posiciones
  (`can_trade()` es `False` fuera de `ACTIVE`).

Confirmar en logs que el worker vio el cambio:

```bash
docker compose logs worker --since 2m | grep -E \
  "bot_state_machine.force_set|cycle_runner.pipeline_aborted_by_state|cycle_runner.paused_by_state"
```

- `bot_state_machine.force_set ... current=KILL_SWITCH_TRIGGERED reason=synced_from_db`
  — el worker sincronizó el estado.
- `cycle_runner.pipeline_aborted_by_state` — abortó el pipeline de decisión a
  mitad de tick.
- `cycle_runner.paused_by_state state=KILL_SWITCH_TRIGGERED` — el loop sigue
  vivo (toca heartbeat, el container queda `healthy`) pero ya no tickea. Este
  es el estado estable esperado tras el kill switch.

Y contra el endpoint:

```bash
curl -s localhost:8000/api/status -H "Authorization: Bearer $TOKEN" \
  | python -c "import json,sys; d=json.load(sys.stdin); print(d['state'], '|', d['state_reason'])"
```

Debe imprimir `KILL_SWITCH_TRIGGERED | <motivo>`.

**Si hay una decisión en curso y no puede esperarse** (ej. incidente grave en
el exchange), no hay forma de acortar el tick desde afuera salvo detener el
proceso — saltar a §2.5 y aceptar el `SIGKILL`. El kill switch ya persistido
garantiza que el estado arrastrado al próximo arranque sea
`KILL_SWITCH_TRIGGERED` (ver §5).

### 2.3 Cancelar órdenes pendientes

**No hay herramienta de operador para esto.** `backend/api/routes_orders.py` es
un stub vacío, sin montar en `backend/app/main.py`. El `PaperAdapter` expone
`get_open_orders(symbol)` y `cancel_order(client_order_id)` (una orden por vez,
solo si está `PENDING`), pero solo desde dentro del proceso worker — su estado
de órdenes vive en memoria (`backend/exchange_adapters/paper_adapter.py`).

Consecuencia en `PAPER`:

- Tras §2.2 el worker no coloca órdenes nuevas.
- Las órdenes `PENDING` que hubiera son objetos en memoria del `PaperAdapter`:
  **se descartan enteras al detener el worker (§2.5)** — no hay nada que
  cancelar explícitamente.
- Lo que persiste son las filas históricas en la tabla `orders` (registro de lo
  que pasó), no órdenes vivas.

Verificación posible hoy (solo lectura, indirecta):

```bash
docker compose logs worker --since 30m | grep -E "execution_engine|order_placed|paper_adapter.order"
```

Para LIVE este paso pasa a ser real y bloqueante — ver §6.

### 2.4 Cerrar posiciones abiertas

**Tampoco hay herramienta de operador.** `backend/api/routes_positions.py` es un
stub vacío, sin montar. No existe `close_all` / `flatten` en ningún módulo. El
cierre de posiciones en operación normal lo hace el `PositionManager` por SL/TP/
trailing/invalidación en cada tick (`backend/position_manager/manager.py`) —
pero tras el kill switch el loop está pausado (`is_running()` es `False`), así
que **el `PositionManager` deja de tickear y no gestiona salidas**.

Consecuencia en `PAPER`:

- El `PositionManager` guarda el `PositionConfig` (SL/TP efectivo, trailing,
  break-even) **en memoria**. Al detener el worker (§2.5) se pierde junto con
  las posiciones simuladas del `PaperAdapter`.
- Persisten las filas de la tabla `positions` y los `trades` cerrados. Una
  posición que quedó "abierta" en DB al momento del apagado queda así: no se
  cierra sola.
- Como no hay dinero real en juego, esto es un problema de consistencia de
  datos, no de riesgo. Al re-arrancar, `ReconciliationGate` detecta la posición
  sin `PositionConfig` como `MISSING_PROTECTION` y dispara `SAFE_MODE`
  automáticamente (ver `docs/runbook_server.md` §3 punto 3 y §6.2).

Si se necesita cerrar una posición en DB de forma explícita antes del apagado
(caso raro, normalmente para dejar el historial limpio), hacerlo a mano contra
Postgres siguiendo el patrón de `docs/runbook_server.md` §6.4 — no hay atajo.

Para LIVE este paso pasa a ser real, bloqueante y con riesgo asociado — ver §6.

### 2.5 Detener los procesos

Emergencia = no esperar al tick en curso. Timeout corto y aceptar el `SIGKILL`
si hace falta:

```bash
docker compose stop -t 30 worker    # worker primero: deja de operar
docker compose stop app             # después la API
# postgres se puede dejar corriendo si el apagado es parcial
```

Qué pasa según cómo termine el worker:

- **`SIGTERM` atendido a tiempo** (`docker-compose.yml` no define
  `stop_grace_period`; el `-t` de arriba lo fija a 30s): los signal handlers
  (`Orchestrator.install_signal_handlers`) llaman `request_shutdown()`, el
  `CycleRunner` sale del loop y `Orchestrator.run()` cierra el `BotRun`
  (`status=STOPPED`) en un `finally`.
- **Timeout agotado → Docker manda `SIGKILL`**: el `finally` no corre, el
  `BotRun` queda `RUNNING` en DB aunque el proceso ya no exista. **No es un
  error grave en emergencia**: el próximo arranque lo cierra como `CRASHED`
  (`Orchestrator._close_orphan_runs`, log `orchestrator.orphan_bot_run_closed`)
  y arrastra el estado `KILL_SWITCH_TRIGGERED` que ya persistió §2.1.

Apagado total del stack:

```bash
docker compose stop -t 30      # los tres servicios, sin borrar contenedores
# o
docker compose down            # además elimina los contenedores (no el volumen)
```

**Nunca `docker compose down -v`** salvo que se quiera borrar la base
(`postgres_data`).

---

## 3. Verificación post-apagado

```bash
docker compose ps                  # estado de los contenedores
docker compose logs worker --tail 50
```

Checklist:

1. `docker compose ps` — `cryptobot-worker` y `cryptobot-app` en `Exited`
   (o ausentes si se hizo `down`).
2. En los logs del worker, uno de:
   - `orchestrator.stopped final_state=KILL_SWITCH_TRIGGERED` +
     `bot_run ... status=STOPPED` → shutdown limpio.
   - Sin línea de `orchestrator.stopped` → murió por `SIGKILL`; el `BotRun`
     quedó `RUNNING` y se cerrará como `CRASHED` en el próximo arranque. Anotar
     que pasó esto.
3. Si Postgres sigue arriba, confirmar el último estado persistido:

   ```bash
   docker compose exec -T postgres psql -U "${POSTGRES_USER:-bot}" -d "${POSTGRES_DB:-cryptobot}" -c \
     "SELECT br.id, br.status, bs.state, bs.reason, bs.created_at
        FROM bot_runs br
        LEFT JOIN LATERAL (
          SELECT state, reason, created_at FROM bot_state
          WHERE bot_run_id = br.id ORDER BY created_at DESC LIMIT 1
        ) bs ON true
       ORDER BY br.started_at DESC LIMIT 3;"
   ```

   El `BotRun` más reciente debe tener `bs.state = KILL_SWITCH_TRIGGERED`. Ese
   estado es el que el próximo arranque va a arrastrar.

---

## 4. Apagado planeado (no emergencia)

Sin kill switch. Se le da al tick en curso el tiempo de terminar.

1. Confirmar que no hay una decisión crítica a mitad de camino:

   ```bash
   docker compose logs -f worker
   # esperar a ver un cycle_runner.heartbeat sin un _process_symbol en vuelo
   ```

2. Detener con timeout generoso — un tick real puede tardar varios minutos
   (varios símbolos × GPT con reintentos):

   ```bash
   docker compose stop -t 300 worker    # ajustar al peor caso real
   docker compose stop app
   docker compose stop postgres          # si es apagado total
   ```

   Con `-t 300`, el `CycleRunner` alcanza a ver la señal de shutdown en la
   espera entre ticks o al tope del `while`, sale del loop y
   `Orchestrator.run()` cierra el `BotRun` como `STOPPED` en su `finally`.

3. Verificar shutdown limpio (§3): debe aparecer
   `orchestrator.stopped` y el `BotRun` en `status=STOPPED`. Si el timeout se
   agotó igual y hubo `SIGKILL`, no es grave — cae en el camino de recuperación
   de crash (`docs/runbook_server.md` §3), solo que era evitable.

**No usar `docker compose kill` ni `kill -9` en un apagado planeado.**

---

## 5. Volver a arrancar después

El re-arranque **no es automático desde `KILL_SWITCH_TRIGGERED`** — es
intencional (PDF 4.8: un estado detenido exige revisión humana, no un restart
de proceso).

- `docker compose up -d` (o `start`) levanta el worker, pero
  `Orchestrator._resolve_carried_over_state` detecta que el último `BotRun` no
  quedó "corriendo" (`is_running()` es `False` para `HALTED` y
  `KILL_SWITCH_TRIGGERED`) y **arrastra ese estado al `BotRun` nuevo**. Log:
  `orchestrator.kill_switch_carried_over`. El bot arranca pausado, no en
  `ACTIVE`.
- `SAFE_MODE` y `MANUAL_PAUSED` **no** se arrastran: un `BotRun` que quedó en
  cualquiera de esos dos nace en `ACTIVE` tras el restart.

Para retomar operación tras un kill switch hay que, en orden:

1. Resolver la causa raíz de la emergencia (logs, `/api/status`, estado real
   del `PaperAdapter`/exchange).
2. Insertar a mano la transición `KILL_SWITCH_TRIGGERED → HALTED` en `bot_state`
   (no hay endpoint), y después `HALTED → ACTIVE` — **dos inserts, uno por uno**.
   Procedimiento exacto con `psql` en `docs/runbook_server.md` §6.4.
3. Reiniciar el worker para que tome el estado `ACTIVE` recién persistido.

---

## 6. Qué cambia con LIVE (pendiente)

Este plan está completo para `PAPER`. Cuando BingX real
(`TESTNET`/`LIVE`) se habilite, los pasos §2.3 y §2.4 dejan de ser "casi N/A" y
pasan a ser reales, bloqueantes y con riesgo de dinero:

- **`BingXAdapter` no está wireado.** `backend/exchange_adapters/bingx_adapter.py`
  ya implementa `cancel_order` (por `clientOrderId`) y `place_order` con
  `reduceOnly`, pero `Orchestrator._prepare_paper_context` solo instancia
  `PaperAdapter` y rechaza cualquier `environment != PAPER`.
- **Falta tooling de operador para órdenes y posiciones.**
  `backend/api/routes_orders.py` y `routes_positions.py` son stubs vacíos sin
  montar. No hay `cancel_all` ni `close_all` / `flatten` en ningún módulo.
  Cancelar N órdenes o cerrar N posiciones requeriría hoy N llamadas manuales
  al adapter desde una consola, sin un comando único.
- **El estado deja de ser efímero.** En LIVE, detener el proceso (§2.5) **no**
  descarta las órdenes ni las posiciones: siguen vivas en el exchange. La
  secuencia kill switch → cancelar órdenes → cerrar posiciones → detener
  procesos pasa a ser obligatoria y en ese orden, y cada paso necesita
  verificación real contra el exchange antes de avanzar al siguiente.

Antes de habilitar LIVE, esta sección y `docs/runbook_server.md` §2/§6 tienen
que revisarse junto con `docs/live_checklist.md`. Tareas mínimas que habilitan
el plan completo:

1. Wirear `BingXAdapter` en el `Orchestrator` para `TESTNET`/`LIVE`.
2. Exponer `routes_orders.py` / `routes_positions.py` con, al menos, `GET`
   (listar vivas) y un `POST` de cancelación/cierre por símbolo, protegidos por
   auth como el kill switch.
3. Un comando o endpoint de "flatten all" que itere el adapter y confirme cada
   cierre, para no depender de N llamadas manuales bajo presión.
