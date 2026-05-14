---
description: Run end-to-end local QA validation for a completed epic of the -10k project
argument-hint: <epic_id> — e.g. F2, F3, F5
---

# /epic-qa — QA de Épica — Proyecto -10k

**Épica a validar**: $ARGUMENTS

Sos un agente de QA autónomo para el proyecto -10k (bot autónomo de futuros crypto).
Tu objetivo es verificar que todo lo entregado en la épica funciona end-to-end en local, según los criterios de aceptación definidos en Trello y el PDF maestro.

Seguí estas fases en orden. No saltees ninguna. Usá `TodoWrite` para rastrear el progreso.

---

## Fase 0 — Parseo y sanity check

1. Extraé el ID de épica de `$ARGUMENTS` (ejemplos válidos: `F2`, `F3`, `F5`). Si está vacío o no tiene formato `F<n>`, detené y pedí al usuario que lo pase correctamente.
2. Definí variables de contexto para usar en el resto del skill:
   - `EPIC_ID` = ID parseado (ej. `F2`)
   - `REPO_DIR` = `~/code/-10k`
   - `REPO_URL` = `https://github.com/aclab-dev/-10k`
   - `BRANCH` = `develop`

---

## Fase 1 — Recolección de contexto (paralela)

Lanzá **3 agentes en paralelo** para reunir todo el contexto antes de planificar los tests:

### Agente A — Contexto Trello
Consultá el board de Trello `-10k` (board ID: `TDbywbhP`) usando las herramientas MCP de Trello disponibles:
- Buscá todas las tarjetas cuyo nombre contenga `[$EPIC_ID]` o `[EPIC $EPIC_ID]`
- Para cada tarjeta encontrada, extraé: nombre, descripción, lista actual (Todo/Doing/Done), checklist items (DoD), comentarios recientes
- Resumí: qué implementó esta épica según Trello, cuál es el DoD completo, qué módulos/componentes crea o modifica

### Agente B — Contexto Git / Código
Trabajando en `$REPO_DIR` (si no existe, cloná desde `$REPO_URL` y hacé checkout de `$BRANCH`):
```bash
# Si el repo no existe:
git clone https://github.com/aclab-dev/-10k.git ~/code/-10k
cd ~/code/-10k && git checkout develop && git pull

# Commits relacionados con la épica:
git log --oneline --all | grep -i "$EPIC_ID\|epic.*$EPIC_ID\|$EPIC_ID.*epic" | head -30

# Archivos tocados por commits de la épica (heurística por mensaje):
git log --pretty=format:"%H" --all | xargs -I{} git show --stat {} 2>/dev/null | grep -B5 -i "$EPIC_ID" | grep "|" | awk '{print $1}' | sort -u | head -40

# Estructura actual del proyecto:
find ~/code/-10k -type f -name "*.py" | grep -v __pycache__ | grep -v ".git" | sort
find ~/code/-10k -name "docker-compose*.yml" -o -name "Dockerfile" | grep -v ".git"
find ~/code/-10k -name "*.yml" -path "*/.github/*" | grep -v ".git"
```
Resumí: qué archivos Python existen, qué módulos se implementaron, qué hay en Docker Compose, qué tests unitarios existen.

### Agente C — Análisis de módulos implementados
Leé los archivos clave del proyecto para entender qué componentes están presentes:
- `~/code/-10k/backend/core/config.py` (si existe) → qué configuraciones hay
- `~/code/-10k/backend/` → estructura de módulos
- `~/code/-10k/worker/` → estructura del worker
- `~/code/-10k/tests/` → qué tests unitarios existen y qué cubren
- `~/code/-10k/docker-compose.yml` → servicios definidos
- `~/code/-10k/.env.example` → variables de entorno requeridas

Resumí: interfaces públicas de cada módulo, endpoints FastAPI registrados, servicios Docker, variables de entorno necesarias.

---

## Fase 2 — Generación del plan de tests

Con la información de los 3 agentes anteriores, construí un **plan de tests específico para esta épica**.

El plan SIEMPRE incluye estos bloques base (ajustá según lo que esté implementado):

### Bloque BASE-1: Prerequisites
Verificar que el entorno local tiene todo lo necesario:
- Docker Desktop corriendo (version 24+)
- Python 3.12+ disponible en PATH
- `uv` instalado
- `curl` disponible
- Puertos necesarios libres (detectar cuáles usa docker-compose.yml)

### Bloque BASE-2: Setup del repo
```bash
cd ~/code/-10k
git checkout develop && git pull
cp .env.example .env  # si no existe .env
```
Ajustar `.env` con valores mínimos para PAPER (sin API keys reales).

### Bloque BASE-3: Calidad estática (siempre)
```bash
cd ~/code/-10k
uv sync --group dev
uv run ruff check .
uv run mypy backend/ worker/   # ajustar paths según lo que exista
uv run pytest -m "not integration" -v
```
Criterio: 0 errores de lint, 0 errores de mypy, todos los unit tests en PASSED.

### Bloque BASE-4: Docker stack (si existe docker-compose.yml)
```bash
docker compose up --build -d
# Esperar hasta que todos los servicios estén healthy
sleep 30 && docker compose ps
```
Criterio: todos los servicios en estado `healthy` o `running`.

### Bloques DINÁMICOS (generados según la épica):

Para cada componente implementado en la épica, generá un bloque de test específico.
Usá la información de los agentes A, B, C para determinar qué testear.

Ejemplos de bloques dinámicos según componente:

**Si existe endpoint `/health` (FastAPI)**:
```bash
curl -s http://localhost:<PORT>/health | python3 -m json.tool
# Criterio: 200 con {"status":"ok","version":"x.x.x","mode":"PAPER"}
```

**Si existe config loader con validaciones**:
```bash
# Test override de env var
docker compose run --rm -e <VAR>=<valor> app python3 -c "from backend.core.config import get_config; cfg = get_config(); print(...)"
# Test de rechazo de config inválida
docker compose run --rm -e <VAR_INVALIDA>=<valor_prohibido> app python3 -c "from backend.core.config import load_config; ..."
```

**Si existe worker con healthcheck**:
```bash
docker inspect <nombre-container-worker> --format='{{.State.Health.Status}}'
# Criterio: healthy
```

**Si existe logging estructurado**:
```bash
docker compose logs app --tail=20
# Criterio: cada línea es JSON válido, sin secrets en texto plano
```

**Si existe módulo de exchange/adapter**:
- Testear que el adapter en modo PAPER no hace llamadas reales
- Testear que el adapter rechaza pares no permitidos

**Si existe Risk Engine**:
- Testear que rechaza trades con margin > 10 USDT
- Testear que rechaza leverage > límite del entorno
- Testear que rechaza cross margin
- Testear que bloquea LIVE sin `I_UNDERSTAND_LIVE_RISK=YES`

**Si existe Decision Aggregator / Quant Signals**:
- Testear que NO_OPERAR y BLOCKED son estados distintos
- Testear que GPT no puede bypassear Risk Engine

**Si existe Backtesting**:
- Correr un backtest con datos históricos mínimos y verificar que genera métricas

**Si existe CI (.github/workflows/)**:
- Verificar que el workflow existe y tiene los pasos: lint + mypy + pytest

### Bloque BASE-5: DoD checklist
Para cada ítem del DoD de la tarjeta Trello, verificá explícitamente que se cumple.
Listá cada ítem con su resultado: ✓ CUMPLE / ✗ FALLA / ~ NO APLICA.

### Bloque TEARDOWN:
```bash
docker compose down
```

---

## Fase 3 — Ejecución

Ejecutá cada bloque del plan en orden. Para cada bloque:
1. Anunciá qué vas a ejecutar y por qué
2. Ejecutá los comandos
3. Evaluá el resultado contra el criterio de éxito
4. Marcá: **PASS** / **FAIL** / **SKIP** (con razón)
5. Si falla: capturá el error exacto. NO modifiques código para hacer pasar el test. Reportá el error tal como está.
6. Actualizá el TodoWrite con el progreso

Si un bloque FAIL bloquea los siguientes (ej. Docker no levanta), marcá los dependientes como SKIP y continuá con los independientes.

---

## Fase 4 — Reporte final

Al terminar todos los bloques, generá un reporte estructurado:

```
═══════════════════════════════════════════
  QA REPORT — EPIC $EPIC_ID — $(date)
═══════════════════════════════════════════

RESUMEN
  Total bloques: X
  PASS:  X
  FAIL:  X
  SKIP:  X

RESULTADOS POR BLOQUE
  [PASS] BASE-1 Prerequisites
  [PASS] BASE-2 Setup repo
  [PASS] BASE-3 Calidad estática
    ruff: 0 errores
    mypy: 0 errores
    pytest: X tests pasaron
  [PASS] BASE-4 Docker stack
    Servicios healthy: postgres, app, worker
  [PASS] DYN-1 Endpoint /health
    Respuesta: {"status":"ok","version":"0.x.0","mode":"PAPER"}
  [FAIL] DYN-2 Config loader — rechazo cross margin
    Error: <mensaje exacto>
    Archivo: <ruta:línea si se puede determinar>
  ...

DoD CHECKLIST
  ✓ Código pasa lint, format y type-check
  ✓ Tests unitarios cubren la lógica nueva
  ✗ Config loader rechaza cross margin [FALLA — ver DYN-2]
  ...

VEREDICTO
  [ APROBADA / RECHAZADA ]
  Razón: <resumen de qué falló o por qué aprueba>

PRÓXIMOS PASOS (si hay FAILs)
  1. <descripción del bug/faltante> → archivo/módulo responsable
  2. ...
```

Si hay FAILs: NO mover la tarjeta Trello a Done. Comentar en la tarjeta con el resumen de errores usando las herramientas MCP de Trello.
Si APROBADA: informar al usuario para que proceda con el PR/merge según las reglas del proyecto.

---

## Reglas de seguridad durante el testing

- No modificar código fuente aunque los tests fallen. Solo reportar.
- No commitear nada durante el QA.
- No habilitar LIVE ni proveer API keys reales. Solo PAPER.
- No hacer `docker compose down -v` (elimina volúmenes). Solo `docker compose down`.
- Si un test requiere credenciales reales, marcarlo como SKIP con nota.
- No loguear ni imprimir contenido de `.env` o variables de entorno con secrets.
