# Runbook del servidor

Procedimientos operativos para correr `-10k` en un servidor mediante `docker
compose`. Cubre arranque, apagado, recuperación tras crash, logs, backups e
incidentes. Todo lo descrito acá corresponde al comportamiento real del código
en `develop` — no son recomendaciones genéricas.

Alcance: entorno `PAPER` en un solo host con Docker Compose (`docker-compose.yml`).
No cubre el despliegue TESTNET/LIVE ni la orquestación multi-nodo; §2.5 resume
qué cambia en el apagado cuando LIVE entre.

Varias secciones documentan un "estado actual" verificado contra `develop`
pero deliberadamente sin resolver (rotación de logs sin límites en
`docker-compose.yml` §4, endpoints de posiciones/órdenes sin montar §6.3). Son
gaps de infra de cara a LIVE, no bugs; §2.5 marca cuáles habilitan la parte
LIVE del apagado de emergencia. Cuando se cierren, revisar §2.5, §4 y §6.3; de
lo contrario quedan desactualizadas en silencio.

---

## 1. Arranque

### 1.1 Primer arranque

```bash
cp .env.example .env
python scripts/hash_password.py   # genera DASHBOARD_PASSWORD_HASH y DASHBOARD_SECRET_KEY
```

Completar en `.env`, como mínimo:

- `DATABASE_URL`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD_HASH`, `DASHBOARD_SECRET_KEY` — la app
  no levanta si falta alguna (fail-closed, `backend/auth/config.py`)
- `OPENAI_API_KEY`
- `ENVIRONMENT=PAPER` (no tocar hasta que TESTNET esté habilitado por checklist)

`DASHBOARD_PASSWORD_HASH` sale del script con cada `$` ya escapado como `$$`.
Pegarlo tal cual — Docker Compose interpola `.env`, y sin el escape el hash
queda corrupto y `cryptobot-app` entra en crash-loop.

### 1.2 Levantar el stack

```bash
docker compose up -d
```

Orden real de arranque (ver `docker-compose.yml`):

1. `postgres` — arranca y espera `pg_isready` (healthcheck cada 5s, hasta 10 intentos).
2. `app` — espera a que `postgres` esté `healthy`, corre `alembic upgrade head` y
   recién después levanta `uvicorn`. Su propio healthcheck (`curl /health`) ya
   corre durante los primeros 15s (`start_period`), pero un fallo en esa
   ventana no cuenta contra `retries` — recién después de esos 15s un fallo
   empieza a sumar hacia el límite que marca al contenedor `unhealthy`.
3. `worker` — arranca en paralelo a `app` en cuanto `postgres` está `healthy`
   (no depende de `app`). Corre `python -m worker.run_worker`.

Verificar que los tres servicios están `healthy`:

```bash
docker compose ps
```

Si `app` queda unhealthy, la causa más común es una migración de Alembic que
falló o el hash del dashboard corrupto — revisar `docker compose logs app`.

### 1.3 Qué hace el worker al arrancar

`worker/run_worker.py` instancia un `Orchestrator` (`backend/trading_core/orchestrator.py`)
antes de entrar al loop. En ese arranque:

- Cierra como `CRASHED` cualquier `BotRun` que haya quedado en `RUNNING` de una
  corrida anterior (ver §3).
- Arrastra al nuevo `BotRun` el último estado persistido si era uno "detenido"
  (`HALTED` o `KILL_SWITCH_TRIGGERED`, según `BotStateMachine.is_running()`) —
  para esos dos el bot **no vuelve a `ACTIVE` solo por reiniciar el proceso**
  (ver §3 y §6). `SAFE_MODE` y `MANUAL_PAUSED` sí cuentan como "corriendo": si
  el `BotRun` anterior quedó en alguno de esos dos, el nuevo arranque **sí
  nace en `ACTIVE`**, sin arrastrar nada.
- Si ya hay otro `BotRun` `RUNNING` (dos workers arrancando a la vez), lanza
  `BotRunAlreadyActiveError`, loguea `worker.bot_run_already_active` y sale con
  código 1 tras dormir `WORKER_STARTUP_RACE_BACKOFF_SECONDS` (300s por defecto).
  `docker compose` (`restart: unless-stopped`) lo reinicia igual, pero el sleep
  espacia los reintentos en vez de loopear cada pocos segundos — **si se ve este
  log, no es un fallo transitorio: hay que investigar por qué dos workers
  compitieron por el mismo `BotRun`, no solo esperar a que se resuelva solo.**

---

## 2. Apagado

Dos caminos según la urgencia:

| Situación | Camino | ¿Interrumpe el tick en curso? |
|---|---|---|
| Deploy, mantenimiento del host, parada ordenada | §2.1 apagado planeado | No — espera a que termine el tick actual |
| Comportamiento anómalo, pérdida fuera de lo esperado, incidente en el exchange o en OpenAI | §2.2 apagado de emergencia | Sí — kill switch primero, procesos después |

La diferencia central: el apagado de emergencia dispara el **kill switch antes
de tocar los procesos**, para que el bot deje de abrir posiciones aunque un
tick largo siga corriendo. El planeado no necesita kill switch — un `SIGTERM`
con timeout generoso alcanza.

### 2.1 Apagado planeado

```bash
docker compose stop        # o: docker compose down (elimina además los contenedores)
```

`docker compose stop` manda `SIGTERM`. `Orchestrator.install_signal_handlers()`
lo conecta a `CycleRunner.request_shutdown()`, que termina el ciclo actual en
curso (no lo interrumpe a mitad de camino) y sale del loop. `Orchestrator.run()`
cierra el `BotRun` (`status=STOPPED`) en un `finally`, así que el cierre limpio
ocurre incluso si el loop terminó por una excepción.

`CycleRunner` solo revisa la señal de shutdown al tope del `while` externo y
durante la espera entre ciclos (`backend/trading_core/cycle_runner.py`) —
**no la revisa a mitad de un tick en curso**. Un tick procesa los símbolos
configurados secuencialmente, cada uno con su propia llamada a GPT (timeout de
30s por intento, hasta 4 intentos con backoff — `backend/decision_engine/gpt_client.py`),
así que un tick real puede tardar varios minutos. El `SIGTERM` no lo acorta:
solo evita que arranque el próximo tick, y el shutdown limpio queda esperando
a que termine el actual.

El timeout por defecto de `docker compose stop` es **10s** (`docker-compose.yml`
no define `stop_grace_period`), muy por debajo de lo que puede tardar un tick
en curso — con ese default, un `stop` a mitad de tick casi siempre termina en
`SIGKILL` igual. Para un apagado planeado, confirmar antes que no hay una
decisión en curso (`docker compose logs -f worker`) y usar un timeout explícito
generoso:

```bash
docker compose stop -t 300 worker   # o el timeout que cubra el peor caso real
docker compose stop app
docker compose stop postgres         # si es apagado total
```

Si aun así se agota el timeout y Docker manda `SIGKILL`, no es un error grave
— cae en el mismo camino de recuperación de un crash (§3), solo que evitable
si se planifica el apagado. **No usar `docker compose kill` ni `kill -9` en un
apagado planeado**: un `SIGKILL` no le da chance al `finally`, el `BotRun`
queda `RUNNING` en la base aunque el proceso ya no exista y el próximo arranque
lo cierra como `CRASHED` (§3) — es el camino de recuperación de una falla, no
el apagado normal.

### 2.2 Apagado de emergencia

Orden: **kill switch → verificar que el worker frenó → cancelar órdenes →
cerrar posiciones → detener procesos**. En `PAPER` los pasos 3 y 4 casi nunca
requieren acción — el estado de órdenes y posiciones simuladas vive en memoria
del worker y se descarta al detenerlo; se documentan igual porque son parte del
DoD de cara a LIVE (§2.5) y porque las filas de DB sí sobreviven.

**Paso 1 — disparar el kill switch.** Necesita un token del dashboard
(`POST /api/auth/login` con `DASHBOARD_USERNAME`/contraseña):

```bash
curl -sX POST localhost:8000/api/kill-switch \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"reason":"<motivo concreto de la emergencia>"}'
```

`backend/api/routes_kill_switch.py` → `EmergencyStopService.trigger`
(`backend/trading_core/emergency_stop.py`) toma lock `FOR UPDATE` sobre el
`BotRun` activo, valida la transición contra la state machine
(`backend/trading_core/bot_state_machine.py`: `ACTIVE`, `SAFE_MODE` y `HALTED`
pueden ir a `KILL_SWITCH_TRIGGERED`) y persiste atómicamente una fila
`bot_state` nueva más un `kill_switch_events` con `requires_manual_review=true`.
Respuesta esperada: `200` con `{"state":"KILL_SWITCH_TRIGGERED", ...}`.

- **409** — el `BotRun` no está `RUNNING`, o la transición no está permitida
  desde el estado actual. Si ya estaba en `KILL_SWITCH_TRIGGERED` o
  `MANUAL_PAUSED`, no hay nada que hacer en este paso.
- **500** — el último `bot_state.state` en DB está fuera del enum (dato
  corrupto). Escalar: es un problema de datos. El worker sigue con su último
  estado en memoria (fail-open, `cycle_runner._sync_state_from_db`).

**Paso 2 — verificar que el worker dejó de operar.** El worker corre en un
proceso separado con su propia `BotStateMachine` en memoria y **no se entera al
instante**: relee `bot_state` al tope de cada iteración del loop y antes de
cada símbolo del pipeline de decisión
(`backend/trading_core/cycle_runner.py::_sync_state_from_db`). Latencia real:
hasta un `WORKER_HEARTBEAT_INTERVAL_SECONDS` (10s por defecto) entre ticks; un
símbolo que ya está a mitad de su llamada a GPT termina antes de frenar, pero
los siguientes del mismo tick ya no abren posiciones (`can_trade()` es `False`
fuera de `ACTIVE`).

```bash
docker compose logs worker --since 2m | grep -E \
  "bot_state_machine.force_set|cycle_runner.pipeline_aborted_by_state|cycle_runner.paused_by_state"
curl -s localhost:8000/api/status -H "Authorization: Bearer <token>" \
  | python -c "import json,sys; d=json.load(sys.stdin); print(d['state'], '|', d['state_reason'])"
```

`cycle_runner.paused_by_state state=KILL_SWITCH_TRIGGERED` en logs y
`KILL_SWITCH_TRIGGERED | <motivo>` en `/api/status` es el estado estable
esperado: el loop sigue vivo (toca heartbeat, container `healthy`) pero ya no
tickea. Si hay una decisión en curso que no puede esperarse, no hay forma de
acortar el tick desde afuera salvo detener el proceso (paso 5) y aceptar el
`SIGKILL` — el kill switch ya persistido garantiza que el estado arrastrado al
próximo arranque sea `KILL_SWITCH_TRIGGERED` (§2.4).

**Paso 3 — cancelar órdenes pendientes.** No hay herramienta de operador:
`backend/api/routes_orders.py` es un stub vacío sin montar (§6.3). En `PAPER`
las órdenes `PENDING` son objetos en memoria del `PaperAdapter` que se
descartan enteras al detener el worker — no hay nada que cancelar
explícitamente; lo que persiste son las filas históricas de la tabla `orders`.
Verificación indirecta:
`docker compose logs worker --since 30m | grep -E "execution_engine|order_placed"`.
Para LIVE este paso pasa a ser real y bloqueante (§2.5).

**Paso 4 — cerrar posiciones abiertas.** Tampoco hay herramienta de operador
(`backend/api/routes_positions.py` es un stub vacío sin montar; no existe
`close_all`/`flatten`). Tras el kill switch el loop está pausado, así que el
`PositionManager` deja de tickear y no gestiona salidas. En `PAPER` el
`PositionConfig` (SL/TP, trailing) vive en memoria y se pierde al detener el
worker junto con las posiciones simuladas; las filas de la tabla `positions`
persisten. Una posición que quedó "abierta" en DB al apagar no se cierra sola:
al re-arrancar, `ReconciliationGate` la detecta como `MISSING_PROTECTION` y
dispara `SAFE_MODE` (§6.2). Como no hay dinero real, es un problema de
consistencia de datos, no de riesgo. Para cerrar una posición en DB de forma
explícita antes del apagado, hacerlo a mano contra Postgres siguiendo el patrón
de §6.4. Para LIVE este paso pasa a ser real, bloqueante y con riesgo (§2.5).

**Paso 5 — detener los procesos.** Emergencia = no esperar al tick en curso.
Timeout corto y aceptar el `SIGKILL`:

```bash
docker compose stop -t 30 worker    # worker primero: deja de operar
docker compose stop app             # después la API
# postgres se puede dejar corriendo si el apagado es parcial
```

Si el `SIGTERM` se atiende a tiempo, los signal handlers cierran el `BotRun`
como `STOPPED`. Si se agota el timeout y entra `SIGKILL`, el `finally` no corre
y el `BotRun` queda `RUNNING`: **no es grave en emergencia** — el próximo
arranque lo cierra como `CRASHED` (§3) y arrastra el `KILL_SWITCH_TRIGGERED`
que ya persistió el paso 1. **Nunca `docker compose down -v`** salvo que se
quiera borrar la base (`postgres_data`).

### 2.3 Verificación post-apagado

```bash
docker compose ps                  # cryptobot-worker y cryptobot-app en Exited (o ausentes si se hizo down)
docker compose logs worker --tail 50
```

En los logs del worker, uno de:

- `orchestrator.stopped final_state=KILL_SWITCH_TRIGGERED` +
  `bot_run ... status=STOPPED` → shutdown limpio.
- sin línea `orchestrator.stopped` → murió por `SIGKILL`; el `BotRun` quedó
  `RUNNING` y se cerrará como `CRASHED` en el próximo arranque. Anotar que pasó.

Si Postgres sigue arriba, confirmar el último estado persistido — el `BotRun`
más reciente debe tener `state = KILL_SWITCH_TRIGGERED` (es el que el próximo
arranque va a arrastrar):

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

### 2.4 Volver a arrancar después de un kill switch

El re-arranque **no es automático desde `KILL_SWITCH_TRIGGERED`** (PDF 4.8: un
estado detenido exige revisión humana). `docker compose up -d` levanta el
worker, pero `Orchestrator._resolve_carried_over_state` arrastra ese estado al
`BotRun` nuevo (log `orchestrator.kill_switch_carried_over`) y el bot arranca
pausado, no en `ACTIVE`. `SAFE_MODE` y `MANUAL_PAUSED` no se arrastran (§3
punto 2). Para retomar operación: resolver la causa raíz, después los **dos
inserts manuales** `KILL_SWITCH_TRIGGERED → HALTED → ACTIVE` en `bot_state`
(uno por uno, no hay endpoint) y reiniciar el worker para que tome el `ACTIVE`
recién persistido. Procedimiento exacto con `psql` en §6.4.

### 2.5 Qué cambia con LIVE

El apagado de emergencia está completo para `PAPER`. Cuando BingX real
(`TESTNET`/`LIVE`) se habilite, los pasos 3 y 4 dejan de ser "casi N/A" y pasan
a ser reales, bloqueantes y con riesgo de dinero:

- **`BingXAdapter` no está wireado.**
  `backend/exchange_adapters/bingx_adapter.py` ya implementa `cancel_order` (por
  `clientOrderId`) y `place_order` con `reduceOnly`, pero
  `Orchestrator._prepare_paper_context` solo instancia `PaperAdapter` y rechaza
  cualquier `environment != PAPER` con `NotImplementedError`.
- **Falta tooling de operador para órdenes y posiciones.** `routes_orders.py` y
  `routes_positions.py` son stubs vacíos sin montar (§6.3). No hay `cancel_all`
  ni `close_all`/`flatten` en ningún módulo.
- **El estado deja de ser efímero.** Detener el proceso (paso 5) ya no descarta
  órdenes ni posiciones: siguen vivas en el exchange. La secuencia kill switch
  → cancelar → cerrar → detener pasa a ser obligatoria en ese orden, con
  verificación real contra el exchange entre pasos.

Tareas mínimas que habilitan el plan completo: wirear `BingXAdapter` para
`TESTNET`/`LIVE`; exponer `routes_orders.py`/`routes_positions.py` con `GET` +
`POST` de cancelación/cierre por símbolo, protegidos por auth como el kill
switch; un "flatten all" que itere el adapter y confirme cada cierre. Revisar
esta sección junto con `docs/live_checklist.md` antes de habilitar LIVE.

---

## 3. Recuperación tras crash

El sistema está diseñado para autodetectar y contener un crash del worker en
el siguiente arranque, sin intervención manual para el caso simple:

1. **`BotRun` huérfano** — `Orchestrator._close_orphan_runs()` marca `CRASHED`
   todo `BotRun` que haya quedado `RUNNING` (probable `SIGKILL` o caída del
   host) y loguea `orchestrator.orphan_bot_run_closed` con su ID. Si aparece
   este log, vale la pena revisar por qué murió el proceso anterior — no es
   ruido esperado en operación normal.
2. **Estado detenido se arrastra — solo `HALTED` y `KILL_SWITCH_TRIGGERED`** —
   si el `BotRun` anterior había quedado en uno de esos dos, el nuevo arranque
   **no vuelve a `ACTIVE` automáticamente**. Se loguea
   `orchestrator.kill_switch_carried_over`. Esto es intencional (PDF 4.8): un
   estado detenido exige revisión humana, no un simple restart de proceso.
   Ver §6 para cómo retomar. **`SAFE_MODE` y `MANUAL_PAUSED` no se arrastran**
   (`BotStateMachine.is_running()` los cuenta como "corriendo"): un `BotRun`
   que quedó en cualquiera de esos dos nace en `ACTIVE` tras un restart. Para
   `SAFE_MODE` esto no dependió nunca del arrastre — ver punto 3: si sigue
   habiendo una condición real (posición sin protección, conectividad), el
   bot vuelve a caer en `SAFE_MODE` solo, por re-detección del propio
   `ReconciliationGate`/`ConnectionHealthMonitor` en el próximo tick, no
   porque el estado anterior haya sobrevivido al restart. `MANUAL_PAUSED` sí
   se pierde sin más: un restart lo reactiva sin ninguna re-detección que lo
   frene.
3. **Posiciones sin protección tras el restart** — `PositionManager` guarda el
   `PositionConfig` (SL/TP efectivo, trailing, break-even) **en memoria**. Un
   restart del worker lo pierde por completo: si había posiciones abiertas,
   quedan sin ningún monitoreo hasta el próximo tick de `ReconciliationGate`,
   que las detecta como `MISSING_PROTECTION` y dispara `SAFE_MODE`
   automáticamente (si `reconciliation.enabled`/`run_before_new_entries` y
   `block_on_unconfirmed_protection` están en `true`). Esa ventana dura como
   máximo un ciclo
   (`WORKER_HEARTBEAT_INTERVAL_SECONDS`, 10s por defecto) — pero **no hay SL/TP
   activo del bot durante esa ventana**. Con posiciones abiertas, verificar
   después de cualquier restart que el bot haya entrado en `SAFE_MODE` como se
   espera, y no asumir que "volvió a arrancar" significa "está protegiendo".

### Checklist post-crash

1. `docker compose ps` — confirmar que los tres servicios volvieron a `healthy`.
2. `docker compose logs worker --since 10m` — buscar `orphan_bot_run_closed` y
   `kill_switch_carried_over`.
3. `curl -s localhost:8000/health` — confirma DB alcanzable y versión corriendo.
4. Si había posiciones abiertas al momento del crash: confirmar que el bot
   quedó en `SAFE_MODE` (`/api/status` expone `state`/`state_reason`, no
   posiciones individuales — no hay endpoint de posiciones hoy, ver §6.3) o
   revisar directamente contra el exchange/`PaperAdapter`. No asumir que
   quedaron protegidas sin revisar.
5. Revisar la causa raíz (OOM, panic de Docker, host reiniciado) antes de
   retomar `ACTIVE` — ver §6.

---

## 4. Rotación de logs

La app loguea JSON estructurado a **stdout** (`backend/core/logging.py`,
`structlog` con `PrintLoggerFactory` + `JSONRenderer`), sin escribir a archivo
propio. Docker captura ese stdout con el driver `json-file` (default de la
instalación de Docker si no se cambió explícitamente), que es el que
efectivamente rota o no.

**Estado actual: `docker-compose.yml` no define límites de `logging:` por
servicio.** Con la config por defecto de Docker, `json-file` puede crecer sin
límite y llenar el disco del host en una corrida larga. Esto no está resuelto
en el código — es una tarea de infraestructura (alineada con F17, "logs y plan
de apagado" de cara a LIVE), no de este runbook. Hasta que se aborde, mitigar
manualmente con una de estas opciones:

**Opción A — límite por servicio en `docker-compose.yml`** (recomendada,
no aplicada todavía):

```yaml
services:
  app:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

Repetir para `worker` y `postgres`. Requiere `docker compose up -d` para
recrear los contenedores con la nueva config — no aplica en caliente.

**Opción B — límite a nivel daemon** (afecta a todos los contenedores del
host, no solo este stack): agregar en `/etc/docker/daemon.json`:

```json
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "5" } }
```

y reiniciar el daemon (`sudo systemctl restart docker`) — reinicia *todos* los
contenedores del host, no solo los de `-10k`.

**Ver logs**: `docker compose logs -f app` / `worker` / `postgres`, o filtrar
por nivel con `docker compose logs app | grep '"level":"error"'` (son líneas
JSON, un objeto por línea).

---

## 5. Backups

No hay script de backup en el repo (`scripts/` no incluye uno) ni volumen
externo configurado más allá del volumen local `postgres_data` de Compose. El
procedimiento es manual:

### 5.1 Backup lógico (`pg_dump`)

```bash
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-bot}" -d "${POSTGRES_DB:-cryptobot}" -F c \
  > "backup_$(date +%Y%m%d_%H%M%S).dump"
```

`-T` es necesario: sin él, `exec` puede asignar un pseudo-TTY y corromper la
salida binaria de `-F c` al redirigirla a un archivo — mismo motivo por el que
el restore de abajo también lo usa. `-F c` (formato custom) permite restore
selectivo y es más chico que un SQL plano. Guardar el archivo fuera del host
(no alcanza con que sobreviva un `docker compose down -v`, que borra el
volumen).

### 5.2 Restore

```bash
docker compose exec -T postgres pg_restore -U "${POSTGRES_USER:-bot}" -d "${POSTGRES_DB:-cryptobot}" \
  --clean --if-exists < backup_YYYYMMDD_HHMMSS.dump
```

Probar el restore contra una base separada antes de asumir que un backup es
válido — un dump que nunca se restauró no es un backup confirmado.

### 5.3 Frecuencia sugerida

Mientras el bot opera en PAPER, un backup diario es suficiente (no hay dinero
real en juego; el valor está en no perder el historial de decisiones/trades
para análisis). Antes de habilitar TESTNET o LIVE, este punto necesita
revisarse junto con el checklist de `docs/live_checklist.md` — backups más
frecuentes y con retención fuera del host pasan a ser requisito, no opción.

### 5.4 Qué NO cubre `pg_dump` solo

Config (`.env`, `config.yaml`) y el snapshot de config que ya viaja dentro de
cada `BotRun.config_snapshot` en la propia tabla — ese sí queda en el dump.
Mantener `.env` y `config.yaml` versionados/respaldados aparte (nunca `.env`
en git, por las API keys).

---

## 6. Manejo de incidentes

### 6.1 Kill switch manual

La secuencia completa de apagado de emergencia (kill switch → verificar que el
worker frenó → cancelar órdenes → cerrar posiciones → detener procesos) está en
**§2.2**, con los códigos de respuesta y la latencia de propagación al worker.

El kill switch aislado —sin detener procesos— es el paso 1 de esa secuencia: un
`POST /api/kill-switch` deja el bot en `KILL_SWITCH_TRIGGERED` para que no abra
posiciones nuevas mientras se investiga, con el worker todavía vivo tocando
heartbeat. Para volver a `ACTIVE` después, §6.4.

### 6.2 SAFE_MODE — quién lo dispara y por qué

Dos componentes tickean en cada ciclo del worker y disparan `SAFE_MODE`
automáticamente ante hallazgos, con el bot en `ACTIVE`:

- **`ReconciliationGate`** (`backend/reconciliation/gate.py`, F16 [159]): corre
  `ReconciliationEngine` (si `reconciliation.enabled`/`run_before_new_entries`
  están en `true`) y bloquea, cada uno gateado por su propio flag en
  `config.yaml`: órdenes activas en el exchange sin fila local
  (`block_on_orphan_orders`), posiciones abiertas sin fila local
  (`block_on_untracked_positions`) o sin `PositionConfig` vigilándolas
  (`block_on_unconfirmed_protection` — ver punto 3 de la sección 3). Unifica lo
  que antes cubría por separado `OrphanOrderScanner` (F16 [115], retirado): su
  detección era un subconjunto estricto de la de `ReconciliationEngine`.
- **`ConnectionHealthMonitor`** (`backend/connection_health/monitor.py`): símbolo
  sin datos de mercado disponibles (`SYMBOL_DATA_UNAVAILABLE`), o clock skew /
  latencia por encima del umbral configurado (`connection_health.max_clock_skew_ms`
  / `max_latency_ms` en `config.yaml`, 2000ms/3000ms por defecto).

Ambos usan el mismo patrón: lock de fila del `BotRun`, transición validada por
la state machine, persistencia atómica de `BotState` + `SystemEvent`. Ninguno
reintenta con contador de ticks: un solo hallazgo ya dispara `SAFE_MODE`,
porque para cuando se reporta ya agotó los reintentos con backoff del propio
fetch. **`SAFE_MODE` bloquea entradas nuevas pero no cierra posiciones
existentes** — a diferencia del kill switch, no es una parada de emergencia,
es "dejar de abrir posiciones nuevas hasta que alguien mire qué pasó".

### 6.3 Diagnóstico rápido

- `curl localhost:8000/health` — `{"status":"ok"}` (DB alcanzable) o
  `{"status":"degraded","db":"unreachable"}` (503).
- `docker compose logs worker --tail 200` — buscar `bot_state_machine.invalid_transition`,
  `orchestrator.orphan_bot_run_closed`, hallazgos de `ReconciliationGate` /
  `ConnectionHealthMonitor` (nombres de evento con prefijo del módulo).
- `/api/status` en el dashboard (requiere auth) para el estado actual del
  `BotRun` (`run_status`, `state`/`state_reason`) y la cuenta agregada
  (balance, equity, PnL, drawdown, exposición). **No expone posiciones
  individuales** — `backend/api/routes_positions.py` y `routes_orders.py`
  existen como stubs vacíos, sin montar en `backend/app/main.py`. Para ver
  posiciones puntuales hoy hay que mirar los logs o el estado del
  `PaperAdapter`/exchange directamente.
- `backend/reconciliation/engine.py` (`ReconciliationEngine`) compara estado
  local vs. exchange (posiciones no registradas, fills parciales, cambios
  manuales, protecciones faltantes) y **ya está wireada al loop del worker**
  (F16 [159], `backend/reconciliation/gate.py::ReconciliationGate`): en cada
  tick de `CycleRunner`, si `reconciliation.enabled` y
  `reconciliation.run_before_new_entries` están en `true` en `config.yaml`, el
  `Orchestrator` corre la reconciliación completa antes de habilitar nuevas
  entradas. Si aparece una orden huérfana (`block_on_orphan_orders`), una
  posición no trackeada (`block_on_untracked_positions`) o una protección no
  confirmada (`block_on_unconfirmed_protection`), el gate dispara `SAFE_MODE`
  vía `EmergencyStopService`, mismo mecanismo que `ConnectionHealthMonitor` —
  buscar en logs `reconciliation_gate.safe_mode_triggered` o el `SystemEvent`
  con `event_type="RECONCILIATION_BLOCKED"`. Un reporte parcial (fetch fallido
  contra el exchange para algún símbolo, `failed_symbols`) no bloquea por sí
  solo — solo lo hacen las 3 condiciones de arriba.
  `manual_balance_change_policy` se lee desde config pero no tiene efecto
  todavía: el engine no detecta discrepancias de balance en su versión actual.

  Para correrla manualmente fuera del loop (ad-hoc, ej. una consola), el
  constructor real (`ReconciliationEngine.__init__`) sigue siendo:

  ```python
  from backend.reconciliation.engine import ReconciliationEngine

  engine = ReconciliationEngine(adapter, position_repo, order_repo, position_manager)
  report = engine.reconcile(bot_run_id)
  # report.is_consistent / report.position_discrepancies / report.order_discrepancies
  ```

  `adapter`, `position_repo` y `order_repo` son requeridos; `position_manager`
  es opcional (default `None`), igual que `symbols` y `decimal_tolerance` —
  pero omitirlo desactiva el chequeo de protecciones (SL/TP faltante o
  divergente), que es justo el hallazgo más interesante del caso. `adapter` es
  el mismo `ExchangeAdapter` (hoy `PaperAdapter`) que usa el worker en curso,
  y `position_repo`/`order_repo`/`position_manager` salen de
  la misma sesión de DB — no instancias nuevas divergentes. Esto sigue siendo
  útil para un chequeo puntual sin esperar al próximo tick; si se necesita con
  frecuencia como comando de operador, vale la pena levantar una tarjeta
  separada para exponerlo como script o endpoint.

### 6.4 Retomar operación tras SAFE_MODE / HALTED / kill switch

No hay auto-resume por diseño (PDF 4.8: `KILL_SWITCH_TRIGGERED` solo degrada a
`HALTED` tras revisión manual). Para retomar:

1. Confirmar la causa raíz del hallazgo (revisar logs, `/api/status`,
   posiciones reales en el exchange/`PaperAdapter`).
2. Resolver lo que haya quedado inconsistente (posición sin protección,
   orden huérfana, conectividad).
3. Reiniciar el worker (`docker compose restart worker`) tiene efecto distinto
   según el estado (ver punto 2 de la sección 3):
   - Si está en `HALTED` o `KILL_SWITCH_TRIGGERED`, el restart **no** lo
     resuelve — esos dos se arrastran al `BotRun` nuevo tal cual, indefinidamente:
     un restart nunca los promueve a `ACTIVE` por sí solo, por más veces que
     se reinicie. Ver el paso 4 de abajo para el único camino real de vuelta.
   - Si está en `SAFE_MODE` o `MANUAL_PAUSED`, el restart **sí** vuelve a
     `ACTIVE` — no hay arrastre. Por eso mismo, **no reiniciar el worker como
     forma de "limpiar" ninguno de los dos sin haber resuelto la causa
     primero**: el restart no vuelve a verificar nada. Para `SAFE_MODE` al
     menos hay red de seguridad — `ReconciliationGate`/`ConnectionHealthMonitor`
     lo pueden volver a detectar en el próximo tick si el problema sigue ahí.
     Para `MANUAL_PAUSED` no hay ninguna: un restart lo reactiva sin que nada
     lo vuelva a frenar.
4. No hay ningún endpoint que escriba `bot_state` salvo `/api/kill-switch`
   (que solo produce `KILL_SWITCH_TRIGGERED`) — ni para bajar
   `KILL_SWITCH_TRIGGERED` a `HALTED`, ni para subir `HALTED` de vuelta a
   `ACTIVE`, aunque la state machine permite ambas transiciones
   (`_ALLOWED_TRANSITIONS`). Un restart del worker **no hace ninguna de las
   dos** — solo arrastra tal cual el último estado persistido (ver punto 3).
   Hoy la única vía para cualquiera de esas dos transiciones es insertar a
   mano una fila nueva en `bot_state`. **`id` y `created_at` no tienen
   `server_default` en la migración** (`migrations/versions/c1edf83a521c_create_all_annexb_tables.py`)
   — son defaults del lado de Python (`_uuid()`/`_now()` en
   `backend/storage/models.py::BotState`) que solo aplican pasando por el
   ORM. Un INSERT crudo sin proveerlos explícitamente falla por `NOT NULL`.
   Ejemplo funcional vía `psql` (Postgres 16 trae `gen_random_uuid()` nativo,
   sin extensión):

   ```bash
   docker compose exec -T postgres psql -U "${POSTGRES_USER:-bot}" -d "${POSTGRES_DB:-cryptobot}" <<'SQL'
   INSERT INTO bot_state (id, bot_run_id, state, previous_state, reason, created_at)
   VALUES (gen_random_uuid(), '<bot_run_id activo>', 'HALTED', 'KILL_SWITCH_TRIGGERED',
           '<motivo de la intervención manual>', now());
   SQL
   ```

   Reemplazar `state`/`previous_state` según la transición (`'ACTIVE'` /
   `'HALTED'` para el segundo paso). Para volver de `KILL_SWITCH_TRIGGERED` a
   `ACTIVE` hacen falta **dos** inserts así, uno por uno (`HALTED` primero,
   `ACTIVE` después) — no hay atajo directo.

### 6.5 Incidentes que no cubre nada de lo anterior

Caída del proveedor de OpenAI, incidente en el exchange, o corrupción de datos
en Postgres no tienen procedimiento automático — requieren intervención
manual siguiendo este runbook (apagado §2, backup/restore §5) y, si
corresponde, kill switch (§2.2 / §6.1) mientras se investiga.
