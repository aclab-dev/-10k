# Cómo apagar el bot — guía rápida

Versión sin tecnicismos del plan de apagado. Para la versión detallada, con el
detalle de qué hace cada componente por dentro, ver `docs/shutdown_plan.md`.

El bot corre en un servidor dentro de tres programas que arrancan juntos:

- **la base de datos** — guarda todo el historial (decisiones, operaciones, estado).
- **la API** — el panel web y los botones (entre ellos, el botón de apagado de emergencia).
- **el worker** — el que realmente opera: mira el mercado, decide y abre o cierra posiciones.

Apagar de forma segura es, sobre todo, **frenar al worker antes de cortarle la
corriente**, para que no quede una operación a medias.

---

## ¿Qué situación tenés?

- **Emergencia** (algo raro está pasando: una pérdida que no cuadra, un
  problema en el exchange, el bot haciendo algo que no debería) → seguí la
  **Parte A**.
- **Apagado planeado** (una actualización, mantenimiento del servidor, parar
  ordenadamente) → seguí la **Parte B**.

La diferencia: en una emergencia se aprieta primero el botón de freno y después
se apagan los programas. En un apagado planeado alcanza con pedirle al worker
que termine lo que está haciendo y se apague solo.

---

## Parte A — Apagado de emergencia

### Paso 1 — Entrar al sistema

Necesitás una "llave" temporal para poder usar los botones. Se pide con este
comando (reemplazá usuario y contraseña por los del panel):

```bash
curl -sX POST localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"TU_USUARIO","password":"TU_CONTRASEÑA"}'
```

Te devuelve un texto largo llamado `access_token`. Guardalo así:

```bash
TOKEN="pegá acá el access_token"
```

### Paso 2 — Mirar cómo está el bot ahora

```bash
curl -s localhost:8000/api/status -H "Authorization: Bearer $TOKEN"
```

Anotá dónde dice `state` (cómo está: operando, pausado, frenado…) y los
números de la cuenta (ganancia/pérdida, plata comprometida). Sirve para
comparar después y para el informe del incidente.

### Paso 3 — Apretar el botón de emergencia (kill switch)

```bash
curl -sX POST localhost:8000/api/kill-switch \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"explicá en una frase qué está pasando"}'
```

Esto le ordena al bot que **deje de abrir operaciones nuevas**. Queda anotado
en el historial con la marca de "necesita revisión de una persona" — o sea, el
bot no va a volver a operar solo aunque después se reinicie el servidor.

Si la respuesta da un error diciendo que ya estaba frenado, está todo bien:
pasá al paso siguiente.

### Paso 4 — Esperar a que el worker se entere

El worker trabaja aparte y **no reacciona en el instante**: puede tardar unos
segundos, o un poco más si justo está esperando la respuesta de la
inteligencia artificial para un símbolo. Esa última consulta puede llegar a
tardar un par de minutos en el peor caso; después de eso, no abre nada más.

Para confirmar que ya frenó, volvé a mirar el estado:

```bash
curl -s localhost:8000/api/status -H "Authorization: Bearer $TOKEN"
```

Cuando en `state` diga que está frenado (kill switch), el worker ya dejó de
operar. Sigue "encendido" pero sin hacer nada — es lo esperado.

Si es una emergencia grave y no podés esperar, saltá directo al Paso 7. El
botón de freno ya quedó registrado, así que el bot no va a retomar solo.

### Paso 5 — Órdenes pendientes

**No hay un botón para esto hoy.** No hace falta preocuparse en el modo actual
(PAPER, sin plata real): las órdenes que estén esperando viven dentro del
worker y desaparecen solas cuando se apaga el programa (Paso 7). En el
historial queda el registro de lo que pasó, que es lo que importa.

### Paso 6 — Posiciones abiertas

**Tampoco hay un botón para esto hoy.** Igual que las órdenes: en el modo
actual, al apagar el worker se cierran solas del lado del simulador. Si al
reiniciar más tarde quedara alguna posición marcada como abierta en el
historial, el propio sistema lo detecta y arranca en modo seguro (sin operar)
hasta que alguien lo revise.

### Paso 7 — Apagar los programas

```bash
docker compose stop -t 30 worker
docker compose stop app
```

Primero el worker (deja de operar), después el panel. Le damos 30 segundos
para que cierre prolijo. Si no llega a cerrar en ese tiempo, el servidor lo
corta a la fuerza — **en una emergencia eso no es un problema**: la próxima vez
que arranque, el sistema se da cuenta y lo deja anotado.

Para apagar todo, incluida la base de datos:

```bash
docker compose stop -t 30
```

**Nunca uses `docker compose down -v`**: eso borra todo el historial guardado.

---

## Parte B — Apagado planeado (sin emergencia)

Acá no hace falta el botón de freno. Se le da tiempo al worker a terminar.

### Paso 1 — Mirar que no haya una decisión a medio camino

```bash
docker compose logs -f worker
```

Vas a ver mensajes pasar. Cuando esté tranquilo (sin una operación en curso),
seguí. Cortá la vista con `Ctrl+C`.

### Paso 2 — Pedirle que se apague, con tiempo de sobra

```bash
docker compose stop -t 300 worker
docker compose stop app
docker compose stop postgres
```

Los 300 segundos (5 minutos) le dan margen para terminar el ciclo que esté
haciendo. Un ciclo puede tardar varios minutos porque consulta a la
inteligencia artificial para cada símbolo, con reintentos.

### Paso 3 — Confirmar

```bash
docker compose ps
```

Los programas deberían figurar como apagados (`Exited`). En los mensajes del
worker debería aparecer una línea de cierre ordenado. Si el tiempo se agotó y
el servidor lo cortó igual, no es grave — solo era evitable.

**En un apagado planeado, no lo cortes a la fuerza a propósito.**

---

## Después de apagar: cómo volver a encender

Encender de nuevo **no vuelve a poner el bot a operar automáticamente** si
antes se usó el botón de emergencia. Es a propósito: un problema serio lo tiene
que revisar una persona, no se arregla solo con reiniciar.

Para que vuelva a operar, alguien con acceso técnico tiene que:

1. Entender y resolver qué causó la emergencia.
2. Habilitar de nuevo la operación a mano (hoy no hay botón; se hace tocando la
   base de datos — el procedimiento está en `docs/shutdown_plan.md`).
3. Reiniciar el worker.

Mientras tanto, si solo reiniciás el servidor, el bot arranca **pausado**, sin
operar. Es la red de seguridad funcionando.

---

## Lo que todavía falta (para cuando se opere con plata real)

Hoy el bot corre en modo simulado. Cuando pase a dinero real, los pasos 5 y 6
(cancelar órdenes y cerrar posiciones) dejan de ser opcionales: en ese modo,
apagar el programa **no** cierra nada, las órdenes y posiciones siguen vivas en
el exchange. Antes de ese cambio hay que agregar:

- La conexión real con el exchange (ya está a medias, falta enchufarla).
- Botones en el panel para ver y cancelar órdenes, y para cerrar posiciones.
- Un botón de "cerrar todo" que no dependa de hacerlo una por una a mano.

Esto está anotado como pendiente y se revisa junto con el checklist de
pre-lanzamiento (`docs/live_checklist.md`).
