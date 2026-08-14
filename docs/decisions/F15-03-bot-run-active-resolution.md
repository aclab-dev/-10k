# F15-03 — Resolución del `bot_run` activo y cierre de corridas huérfanas

**Estado**: Aceptada  
**Fecha**: 2026-08-13  
**Epic**: F15 — Dashboard  

---

## Contexto

`BotRunRepository.get_active()` resolvía el "run activo" con `limit(1)` sin `order_by`. Con más de una fila en `RUNNING`, cuál de ellas devuelve queda a criterio del planner de PostgreSQL y puede cambiar entre llamadas.

Que haya más de una fila en `RUNNING` no es hipotético. `Orchestrator._prepare_paper_context` crea un `BotRun` nuevo en cada arranque del worker, y el anterior se marca `STOPPED` en el `finally` de `Orchestrator.run()`. Ese `finally` no corre si el proceso muere por SIGKILL (OOM, `docker kill`, corte de energía): el run viejo queda colgado en `RUNNING` para siempre y el arranque siguiente agrega otro.

Desde [F15] eso tiene consecuencias operativas. `get_current_bot_run` (`/api/status`, `/api/kill-switch`) resuelve el run vía `get_active()`, mientras que `CycleRunner._sync_state_from_db` filtra por su propio `bot_run_id`. Si la API elige el run viejo, el operador aprieta Kill Switch, el dashboard muestra `KILL_SWITCH_TRIGGERED` y el bot sigue operando. Peor que no tener kill switch: da confianza falsa justo cuando se la necesita.

Detectado durante la review del PR [#108](https://github.com/aclab-dev/-10k/pull/108); el bug es anterior a ese PR.

---

## Decisión

### 1. `get_active()` desempata por `started_at desc`

Mismo criterio que `get_most_recent()`: el run activo es siempre el más reciente. Es lo que corresponde semánticamente — el worker vivo es el que arrancó último — y elimina la dependencia del planner.

### 2. Además se cierran los `RUNNING` huérfanos al arrancar el worker

`BotRunRepository.close_orphan_running()` marca `CRASHED` todo `BotRun` que siga en `RUNNING`, y `Orchestrator._prepare_paper_context` lo invoca antes de crear el run nuevo.

Solo desempatar en la lectura deja la base violando permanentemente el invariante "a lo sumo un `bot_run` en `RUNNING`". Las filas colgadas se acumularían, y cualquier otro consumidor que asuma el invariante —reportes, queries futuras, un `/api/status` consultado con `bot_run_id` explícito— seguiría roto aunque `get_active()` esté arreglado. Un invariante que solo vale dentro de una función no es un invariante: es un parche en el único lugar donde hoy se nota.

### 3. Las dos cosas, no una

El `order_by` no queda redundante. Entre que el worker nuevo cierra los huérfanos y termina de insertar su propio `bot_run` hay una ventana en la que un lector concurrente ve dos filas `RUNNING`. El desempate determinístico cubre esa ventana y cualquier otra que aparezca; el cierre de huérfanos evita que la ventana se vuelva permanente.

### 4. Status `CRASHED`, no `STOPPED`

`STOPPED` afirma que hubo shutdown limpio, y estas corridas son exactamente las que no lo tuvieron. Reusarlo borraría la única evidencia de que el proceso murió mal, que es justo lo que hay que poder ver al investigar. `bot_runs.status` es `String(32)` sin constraint, y el frontend lo muestra como texto (`Status.tsx`), así que no rompe ningún contrato.

`ended_at` queda en el momento en que se detecta el huérfano, no en el de la muerte real —que nadie registró—. El motivo se anota en `BotRun.notes` y se loguea un `warning` estructurado (`orchestrator.orphan_bot_run_closed`) por cada huérfano cerrado: que haya uno significa que la corrida anterior murió mal, y eso amerita mirar por qué.

### 5. El cierre corre después de resolver el carry-over

`_resolve_carried_over_state` usa `get_most_recent()` (cualquier status) y el `bot_state` del run anterior. Ninguno de los dos depende de `status`, así que el orden entre ambos no cambia el resultado; se deja el cierre después para que el bloque que resuelve el estado arrastrado siga leyéndose como una sola cosa. Va en el mismo `commit()` que agrupa el run nuevo y su `bot_state` arrastrado, sin agregar transacciones.

---

## Consecuencias

- Aparece un valor nuevo posible en `bot_runs.status`: `CRASHED`. Es informativo; ningún consumidor ramifica por status hoy.
- El invariante "a lo sumo un `bot_run` en `RUNNING`" pasa a sostenerse en la base, no solo en la lectura.
- Sin cambios de esquema ni migración: se reusan las columnas `status`, `ended_at` y `notes` existentes.

### Supuesto explícito: un solo worker

Si dos workers corrieran en paralelo contra la misma base, el segundo en arrancar marcaría `CRASHED` el run del primero, que sigue vivo. Hoy el deploy es un único servicio `worker` en Docker Compose, y el carry-over del kill switch ya asume ese mismo dueño único del estado. El día que haya más de un worker esto necesita revisión —lo natural sería identificar al proceso dueño de cada run (hostname/PID) y cerrar solo los que no tienen proceso vivo—, junto con el resto de los supuestos de dueño único.

---

## Alternativas descartadas

**Solo el `order_by`**: es el cambio mínimo que arregla el síntoma reportado, pero convive con la violación del invariante en vez de resolverla (ver punto 2).

**Cerrar huérfanos como `STOPPED`**: menos valores de status para mantener, pero pierde la distinción entre un shutdown limpio y un proceso que murió sin ejecutar su `finally` — que es la información que se busca cuando se investiga por qué había dos runs.

**Un constraint único parcial (`UNIQUE ... WHERE status = 'RUNNING'`)**: haría que la base rechace la segunda fila `RUNNING`. Descartado porque convierte un arranque tras un SIGKILL en un fallo al insertar el run nuevo: el worker no podría arrancar hasta que alguien limpie a mano. La limpieza automática al arrancar resuelve el mismo problema sin dejar al bot sin poder levantarse.
