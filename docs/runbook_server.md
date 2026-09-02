# Runbook del servidor

Procedimientos operativos para correr `-10k` en un servidor mediante `docker
compose`. Cubre arranque, apagado, recuperación tras crash, logs, backups e
incidentes. Todo lo descrito acá corresponde al comportamiento real del código
en `develop` — no son recomendaciones genéricas.

Alcance: entorno `PAPER` en un solo host con Docker Compose (`docker-compose.yml`).
No cubre despliegue TESTNET/LIVE ni orquestación multi-nodo — eso es F17/F18.

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

## 2. Apagado seguro

```bash
docker compose stop        # o: docker compose down (elimina además los contenedores)
```

`docker compose stop` manda `SIGTERM`. `Orchestrator.install_signal_handlers()`
lo conecta a `CycleRunner.request_shutdown()`, que termina el ciclo actual en
curso (no lo interrumpe a mitad de camino) y sale del loop. `Orchestrator.run()`
cierra el `BotRun` (`status=STOPPED`) en un `finally`, así que el cierre limpio
ocurre incluso si el loop terminó por una excepción.

**No usar `docker compose kill` ni `kill -9` sobre el proceso salvo emergencia
real.** Un `SIGKILL` no le da chance al `finally` de correr: el `BotRun` queda
en `RUNNING` en la base aunque el proceso ya no exista, y el próximo arranque
lo detecta y cierra como `CRASHED` (ver §3) — funciona, pero es el camino de
recuperación de una falla, no el apagado normal.

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
`SIGKILL` igual. Para un apagado planeado, usar un timeout explícito generoso:

```bash
docker compose stop -t 300 worker   # o el timeout que cubra el peor caso real
```

Si aun así se agota el timeout y Docker manda `SIGKILL`, no es un error grave
— cae en el mismo camino de recuperación de un crash (§3), solo que evitable
si se planifica el apagado. Antes de un apagado planeado, confirmar que no hay
una decisión en curso mirando los logs (`docker compose logs -f worker`).

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
   `OrphanOrderScanner`/`ConnectionHealthMonitor` en el próximo tick, no
   porque el estado anterior haya sobrevivido al restart. `MANUAL_PAUSED` sí
   se pierde sin más: un restart lo reactiva sin ninguna re-detección que lo
   frene.
3. **Posiciones sin protección tras el restart** — `PositionManager` guarda el
   `PositionConfig` (SL/TP efectivo, trailing, break-even) **en memoria**. Un
   restart del worker lo pierde por completo: si había posiciones abiertas,
   quedan sin ningún monitoreo hasta el próximo tick de `OrphanOrderScanner`,
   que las detecta como `UNPROTECTED_POSITION` y dispara `SAFE_MODE`
   automáticamente. Esa ventana dura como máximo un ciclo
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

```bash
curl -sX POST localhost:8000/api/kill-switch \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"reason":"<motivo>"}'
```

Transiciona el `BotState` a `KILL_SWITCH_TRIGGERED` (`backend/api/routes_kill_switch.py`).
El worker corre en un proceso separado y no se entera al instante: relee
`bot_state` al tope de cada iteración del loop y antes de cada símbolo del
pipeline de decisión — en el peor caso, un símbolo que ya está a mitad de una
llamada a GPT (hasta ~30s + reintentos) termina de procesarse antes de frenar,
pero no se abren posiciones nuevas para los símbolos siguientes del mismo ciclo.

Obtener token: `POST /api/auth/login` con `DASHBOARD_USERNAME`/contraseña.

### 6.2 SAFE_MODE — quién lo dispara y por qué

Dos componentes tickean en cada ciclo del worker y disparan `SAFE_MODE`
automáticamente ante hallazgos, con el bot en `ACTIVE`:

- **`OrphanOrderScanner`** (`backend/orphan_order_scanner/`): órdenes activas en
  el exchange sin fila local (`UNEXPLAINED_ORDER`) o posiciones abiertas sin
  `PositionConfig` vigilándolas (`UNPROTECTED_POSITION` — ver punto 3 de la
  sección 3).
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
  `orchestrator.orphan_bot_run_closed`, hallazgos de `OrphanOrderScanner` /
  `ConnectionHealthMonitor` (nombres de evento con prefijo del módulo).
- `/api/status` en el dashboard (requiere auth) para el estado actual del
  `BotRun` (`run_status`, `state`/`state_reason`) y la cuenta agregada
  (balance, equity, PnL, drawdown, exposición). **No expone posiciones
  individuales** — `backend/api/routes_positions.py` y `routes_orders.py`
  existen como stubs vacíos, sin montar en `backend/app/main.py`. Para ver
  posiciones puntuales hoy hay que mirar los logs o el estado del
  `PaperAdapter`/exchange directamente.
- `backend/reconciliation/engine.py` (`ReconciliationEngine`) compara estado
  local vs. exchange bajo demanda (posiciones no registradas, fills parciales,
  cambios manuales, protecciones faltantes) — **hoy es una clase de librería,
  no wireada al loop del worker ni expuesta por script o endpoint** (existe
  `reconciliation.run_before_new_entries` en `config.yaml`, pero nada lo lee
  todavía). Para correrla manualmente hoy hace falta un script ad-hoc, no hay
  un comando listo — instanciarla requiere exactamente esta firma:

  ```python
  from backend.reconciliation.engine import ReconciliationEngine

  engine = ReconciliationEngine(adapter, position_repo, order_repo, position_manager)
  report = engine.reconcile(bot_run_id)
  # report.is_consistent / report.position_discrepancies / report.order_discrepancies
  ```

  `adapter` es el mismo `ExchangeAdapter` (hoy `PaperAdapter`) que usa el
  worker en curso, y `position_repo`/`order_repo`/`position_manager` salen de
  la misma sesión de DB — no instancias nuevas divergentes. Si se necesita
  este chequeo con frecuencia, vale la pena levantar una tarjeta separada
  para exponerlo como script o endpoint en vez de repetir este ad-hoc cada vez.

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
     menos hay red de seguridad — `OrphanOrderScanner`/`ConnectionHealthMonitor`
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
   mano una fila nueva en `bot_state` (tabla `backend/storage/models.py::BotState`
   — columnas `bot_run_id`, `state`, `previous_state`, `reason`,
   `created_at`) con el `state` destino para el `bot_run_id` activo, dejando
   `reason` con el motivo de la intervención para la auditoría. Es decir: para
   volver de `KILL_SWITCH_TRIGGERED` a `ACTIVE` hacen falta **dos** inserts
   manuales separados (`HALTED` primero, `ACTIVE` después), no uno.

### 6.5 Incidentes que no cubre nada de lo anterior

Caída del proveedor de OpenAI, incidente en el exchange, o corrupción de datos
en Postgres no tienen procedimiento automático — requieren intervención
manual siguiendo este runbook (apagado seguro §2, backup/restore §5) y, si
corresponde, kill switch (§6.1) mientras se investiga.
