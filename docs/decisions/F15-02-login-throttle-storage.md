# F15-02 — Backend del contador de rate limiting del login

**Estado**: Aceptada  
**Fecha**: 2026-08-12  
**Epic**: F15 — Dashboard  

---

## Contexto

La tarjeta [152] pide rate limiting y lockout de brute-force en `POST /api/auth/login`, y deja explícitamente abierta una decisión: dónde vive el contador de intentos fallidos. In-memory contra PostgreSQL, y en el segundo caso, si amerita tabla nueva o alcanza con `system_events`.

Estado al momento de esta decisión:

- La tarjeta [109] implementó login + middleware y dejó el throttling fuera de scope: hoy el endpoint acepta intentos ilimitados.
- Cada intento cuesta una derivación scrypt (~100 ms, ~64 MB). El costo frena el brute-force pero también convierte al endpoint en un vector de DoS barato.
- El deploy es Docker Compose con `postgres`, `app` y `worker`. uvicorn puede escalar workers dentro del container de `app`.
- `system_events` tiene `bot_run_id` como FK NOT NULL a `bot_runs`.

---

## Decisión

### 1. El contador vive en PostgreSQL, en dos tablas nuevas

`login_attempts` (un registro por fallo, para la ventana deslizante) y `login_lockouts` (estado del bloqueo y memoria del backoff, con unique en `(scope, identifier)`).

Ambas quedan **fuera del Anexo B**: son operativas del dashboard, no del dominio de trading.

### 2. Se descarta in-memory

Un contador por proceso es evadible levantando procesos y se pierde en cada reinicio — que es algo que un atacante puede provocar. El spec §3.5.7 elige PostgreSQL desde el inicio precisamente por "auditoría, consistencia transaccional y reconstrucción completa"; un límite de seguridad que no sobrevive un restart no cumple ninguna de las tres.

### 3. Se descarta `system_events`

Su `bot_run_id` es FK NOT NULL a `bot_runs`. Un intento de login no pertenece a ninguna corrida del bot: usarla exigiría relajar el schema del Anexo B o inventar un `bot_run` sintético. Además `system_events` es auditoría del bot de trading, y mezclarle eventos de la superficie web degrada su valor como registro reconstruible.

### 4. Dos scopes independientes: usuario e IP

El límite por usuario frena el ataque dirigido a una cuenta; el de IP, el barrido de usuarios desde un mismo origen. El username se normaliza con `casefold` para que alternar mayúsculas no multiplique la cuota.

### 5. El chequeo de lockout corre antes de verificar la password

Una identidad bloqueada cuesta un SELECT indexado: no paga scrypt ni escribe. Sin esto, el lockout frenaría el brute-force pero no el DoS, que es la mitad del problema que plantea la tarjeta.

### 6. La sección crítica se serializa con un advisory lock de PostgreSQL

Contar los fallos y decidir si se bloquea son dos pasos. Bajo READ COMMITTED, varias transacciones concurrentes leen el conteo antes de que las otras commiteen. Verificado empíricamente contra Postgres real: **12 fallos concurrentes con umbral 3 producen cero lockouts** sin serialización. No es un exceso acotado, es un bypass del throttle durante todo el burst.

`pg_advisory_xact_lock(scope_namespace, hash(identifier))` se toma por identidad antes de contar, después de scrypt, y se libera al commitear. Es específico de PostgreSQL, que es la base obligatoria del proyecto; en SQLite (tests unitarios) es un no-op y la cobertura real vive en `tests/integration/test_login_throttle_concurrency.py`, sobre el contenedor de Postgres.

Esto sigue el patrón que ya fijó [F10-05](F10-05-order-idempotency.md): la garantía la da la base, no una verificación en código de aplicación entre un SELECT y una decisión.

### 7. Retención

- `login_attempts`: se purgan al salir de la ventana de conteo.
- `login_lockouts`: se purgan cuando vencieron hace más que el mayor entre `window_seconds` y `max_lockout_seconds`. No se borran apenas vencen: `lockout_count` es la memoria del backoff, y perderla le devolvería el bloqueo mínimo a quien reincide.

---

## Consecuencias

### Cambios de datos

- Tablas nuevas `login_attempts` y `login_lockouts` (migración `b4d17a90c3e5`).
- Índice compuesto `(scope, identifier, timestamp)` para el conteo, más uno sobre `timestamp` solo, que es como filtra la purga.

### Requisito de infra

El límite por IP depende de que `request.client` sea la IP real del cliente. uvicorn aplica `X-Forwarded-For` solo para peers en `forwarded_allow_ips` (default `127.0.0.1`). **Detrás de un reverse proxy que no sea localhost desde la perspectiva de uvicorn hay que setear `FORWARDED_ALLOW_IPS`**; sin eso, todos los clientes comparten la IP del proxy y unos pocos fallos bloquean a todos. Nunca `*`: habilita a cualquiera a falsear su IP y saltearse el límite.

### Trade-off aceptado

Con lockout por usuario, un atacante puede dejar afuera al operador legítimo durante `lockout_seconds`. Es inherente a lo que pide la tarjeta y se acota con la duración configurable. El spec §2 ("Seguridad primero: ninguna rentabilidad potencial justifica violar límites de riesgo, seguridad o auditoría") ordena la prioridad: preferimos un operador esperando 5 minutos a un endpoint sin límite. La superficie afectada tiene poder limitado por diseño (§4.15: el dashboard no habilita LIVE ni saltea el Risk Engine).

### Lo que NO cambia

- El contrato de `/api/auth/login` para el caso feliz: mismo 200, mismo payload.
- El frontend: `Login.tsx` ya muestra el `detail` de cualquier `ApiError`, así que el 429 se ve sin tocar nada.
- Hashing, tokens y el middleware `require_auth` de [109].

---

## Alternativas descartadas

**Contador in-memory con TTL**: cero latencia y cero esquema, pero no sobrevive reinicios ni se comparte entre workers. Descartado por el spec §3.5.7 y por ser trivialmente evadible.

**Reusar `system_events`**: evitaba tablas nuevas, pero exige `bot_run_id` y mezcla auditoría de trading con la superficie web.

**Sin serialización, aceptando el exceso**: se evaluó documentar la carrera como límite conocido. La medición contra Postgres real mostró que no es un exceso acotado sino un bypass completo durante el burst, así que se descartó.

**`hashtext()` de PostgreSQL para la clave del lock**: es una función interna sin contrato de estabilidad entre versiones. Se hashea en Python con `crc32`.
