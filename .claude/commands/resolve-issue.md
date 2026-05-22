---
description: Full development workflow from a GitHub issue — find the issue, plan, implement, self-review loop, then request review from Rodrigo. Use when the user says "take this issue", "work on issue #X", "implement issue #X", "tomá el issue", "arrancá con el issue", or provides a GitHub issue URL or number to develop.
argument-hint: GitHub issue number or full URL (e.g. 42 or https://github.com/aclab-dev/-10k/issues/42)
---

# /resolve-issue — Flujo completo de desarrollo desde un issue de GitHub

**Issue a trabajar**: $ARGUMENTS

Sos un agente de desarrollo autónomo para el proyecto -10k. Tu objetivo es tomar un issue de GitHub, implementarlo según las reglas del proyecto, hacer self-review hasta que el código esté listo, y solicitar review a Rodrigo.

Seguí estas fases en orden estricto. No avancés sin completar la fase anterior.

---

## Definición de sub-agentes

### Sub-agente GitHub (GH-AGENT)
Este sub-agente tiene una responsabilidad única: **operar sobre GitHub Issues y PRs** usando el MCP server de GitHub configurado en el sistema.

Capacidades:
- Buscar un issue por número o URL en `https://github.com/aclab-dev/-10k`
- Leer título, descripción, labels, comentarios y estado de un issue
- Agregar label `in-progress` al issue cuando el desarrollo comienza
- Crear un Pull Request contra `develop` con el formato del proyecto
- Asignar `rodrigosanchez` como reviewer en el PR

Reglas de operación:
- Repo base: `https://github.com/aclab-dev/-10k`
- Branch base para PRs: `develop`
- Si el MCP de GitHub no responde, reportar el error y detener el flujo

### Sub-agente Self-Review (SR-AGENT)
Este sub-agente tiene una responsabilidad única: **revisar el código producido en el feature branch** antes de solicitar review humano.

El SR-AGENT recibe:
- El título y descripción del issue (los requisitos)
- El nombre del feature branch
- El diff completo entre el feature branch y `develop`

El SR-AGENT evalúa:
1. **Correctitud funcional**: el código cumple lo pedido en el issue
2. **Reglas del proyecto** (lista de verificación):
   - Type hints en todo código nuevo
   - Sin secretos, API keys ni endpoints hardcodeados
   - Sin `except Exception: pass` ni errores silenciados
   - PostgreSQL + SQLAlchemy si hay acceso a datos (no SQLite)
   - UUID en decisiones/órdenes si aplica (idempotencia)
   - Tests unitarios cubren la lógica nueva
   - `ruff`, `black`, `mypy` pasarían sin errores (revisión visual)
   - Logging JSON estructurado, sin PII en logs
   - Ningún archivo `.env` ni credencial en el diff
3. **Calidad de código**: nombres claros, funciones puras donde posible, sin abstracciones prematuras
4. **Impacto en módulos existentes**: no degrada contratos previos

El SR-AGENT devuelve UNO de los dos veredictos siguientes:

**APROBADO**: el código cumple todos los criterios. Incluir un resumen de qué revisó.

**FIX REQUERIDO**: incluir lista numerada de problemas específicos, cada uno con:
- Archivo y línea aproximada
- Descripción del problema
- Qué se debe corregir

---

## Fase 0 — Parseo del input

1. Parsear `$ARGUMENTS` para extraer el número de issue:
   - Si es URL completa (`https://github.com/aclab-dev/-10k/issues/42`) → extraer `42`
   - Si es número puro (`42`) → usar directamente
   - Si está vacío o no tiene formato reconocible → detener y pedir al usuario que lo pase correctamente
2. Definir variables de contexto:
   - `ISSUE_NUMBER` = número extraído
   - `REPO` = `aclab-dev/-10k`
   - `REPO_DIR` = `~/code/-10k`
   - `BASE_BRANCH` = `develop`

---

## Fase 1 — Búsqueda del issue (GH-AGENT)

Invocar el **GH-AGENT** con la siguiente tarea:

> Usando el MCP server de GitHub, buscar el issue #`ISSUE_NUMBER` en el repositorio `aclab-dev/-10k`.
> Retornar: título, descripción completa, labels actuales, estado (open/closed), y si tiene algún assignee.
> Si el issue no existe o el MCP devuelve error → reportar el error exacto.

**Si el issue no existe**: detener el flujo y notificar al usuario con el mensaje:
```
Issue #ISSUE_NUMBER no encontrado en aclab-dev/-10k. Verificá el número e intentá de nuevo.
```

**Si el issue existe**:
- Guardar `ISSUE_TITLE` y `ISSUE_BODY` para usarlos en las fases siguientes
- Inferir el slug del branch: tomar las primeras 4-5 palabras del título, en kebab-case, sin números de issue (ej. "Add config loader for paper env" → `config-loader-paper`)
- Definir `FEATURE_BRANCH` = `feature/F<número>-<slug>` si el título tiene prefijo `[F<n>]`, o `feature/issue-<ISSUE_NUMBER>-<slug>` si no

---

## Fase 2 — Planificación

Con el contenido del issue, generar un plan de implementación que incluya:

1. **Entendimiento del issue**: qué pide exactamente, qué módulos toca, qué no toca
2. **Dependencias**: qué debe estar implementado antes (verificar según reglas del proyecto)
3. **Pasos de implementación**: lista ordenada de cambios concretos (archivos a crear/modificar, lógica a implementar)
4. **Tests a escribir**: qué casos cubre cada test
5. **DoD checklist**: cómo se verificará que el issue está resuelto
6. **Riesgos o dudas**: si algo en el issue es ambiguo, listarlo

Presentar el plan completo al usuario y **esperar confirmación explícita** antes de continuar.

Mensaje de espera:
```
Plan listo. ¿Arrancamos? (confirmá con "sí", "ok", "adelante" o pedí cambios)
```

**Iterar** con el usuario hasta recibir confirmación. No avanzar sin OK explícito.

---

## Fase 3 — Setup del branch

Una vez recibida la confirmación del usuario:

1. Asegurarse de que el repo local existe. Si no:
   ```bash
   git clone https://github.com/aclab-dev/-10k.git ~/code/-10k
   ```

2. Actualizar `develop`:
   ```bash
   cd ~/code/-10k
   git checkout develop
   git pull origin develop
   ```

3. Crear el feature branch:
   ```bash
   git checkout -b FEATURE_BRANCH
   ```

4. Invocar el **GH-AGENT** para marcar el issue como en progreso:
   > Agregar el label `in-progress` al issue #`ISSUE_NUMBER` en `aclab-dev/-10k`.
   > Si el label no existe, crearlo con color `#0075ca`.

Notificar al usuario:
```
Branch FEATURE_BRANCH creado desde develop. Iniciando desarrollo...
```

---

## Fase 4 — Desarrollo

Implementar los cambios según el plan aprobado.

Reglas durante el desarrollo:
- Una cosa a la vez. Completar cada paso del plan antes de avanzar al siguiente.
- Type hints obligatorios en todo código nuevo.
- Escribir tests junto con el código, no al final.
- Sin `git add .` ni `git add -A`. Staging explícito archivo por archivo.
- Commits pequeños con mensaje en infinitivo en inglés, formato: `Add/Fix/Update <qué>`
- No tocar archivos fuera del scope del issue.
- Si durante el desarrollo aparece algo que excede el scope → no implementarlo. Notificar al usuario para crear una tarjeta separada.

Al finalizar el desarrollo, hacer un commit final si quedan cambios sin commitear.

---

## Fase 5 — Self-Review (SR-AGENT)

Invocar el **SR-AGENT** con la siguiente información:

> **Issue**: #`ISSUE_NUMBER` — `ISSUE_TITLE`
> **Descripción del issue**: `ISSUE_BODY`
> **Branch**: `FEATURE_BRANCH`
> **Diff a revisar**: (obtener con `git diff develop...FEATURE_BRANCH` en `REPO_DIR`)
>
> Revisar el código según los criterios del SR-AGENT definidos al inicio de este skill.
> Retornar APROBADO o FIX REQUERIDO con detalle.

**Si SR-AGENT retorna FIX REQUERIDO**:
1. Listar los problemas al usuario con claridad
2. Aplicar los fixes necesarios para resolver cada punto
3. Hacer commit de los fixes
4. Volver al inicio de la Fase 5 (nueva iteración de self-review)
5. Repetir hasta recibir APROBADO

Límite: máximo 3 ciclos de self-review. Si después de 3 ciclos aún hay problemas, detener y pedir orientación al usuario.

**Si SR-AGENT retorna APROBADO**: avanzar a Fase 6.

---

## Fase 6 — Pull Request y solicitud de review (GH-AGENT)

Invocar el **GH-AGENT** con la siguiente tarea:

> Crear un Pull Request en `aclab-dev/-10k` con los siguientes datos:
>
> - **Title**: `[Issue #ISSUE_NUMBER] ISSUE_TITLE`
> - **Base branch**: `develop`
> - **Head branch**: `FEATURE_BRANCH`
> - **Body**:
>   ```
>   ## Issue
>   Closes #ISSUE_NUMBER
>
>   ## Cambios
>   (resumen de los cambios implementados — 3-5 bullets)
>
>   ## Cómo testear
>   (pasos concretos para verificar que funciona)
>
>   ## DoD checklist
>   - [ ] Código pasa lint, format y type-check
>   - [ ] Tests unitarios cubren la lógica nueva
>   - [ ] Sin secretos ni endpoints hardcodeados
>   - [ ] No rompe tests existentes
>   - [ ] No degrada contratos de módulos previos
>   ```
> - **Reviewer**: `rodrigosanchez`
>
> Retornar la URL del PR creado.

Notificar al usuario con la URL del PR y el resumen:
```
PR creado: <URL>
Self-review: APROBADO
Reviewer asignado: @rodrigosanchez
```

---

## Reglas de seguridad globales

- Nunca loguear, imprimir ni commitear API keys, tokens ni secretos.
- Nunca hacer push directo a `main` o `develop`.
- Nunca usar `--force`, `--no-verify` ni `--force-with-lease` salvo pedido explícito.
- Nunca mergear el PR (eso es responsabilidad del humano tras la review).
- Si cualquier paso toca una regla no negociable del proyecto (-10k rules §2), detener y pedir confirmación al usuario.
