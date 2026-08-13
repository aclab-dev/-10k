# F15-01 — Stack Frontend: Dashboard

**Estado**: Aceptada  
**Fecha**: 2026-07-27  
**Actualizada**: 2026-08-13 — se agrega la sección "Desviación respecto de la spec (§4.11)" ([153](https://trello.com/c/jWofqdS8/153-153-f15-alinear-el-scaffolding-del-frontend-con-el-adr-f15-01))  
**Epic**: F15 — Dashboard  

---

## Contexto

El bot necesita un dashboard de monitoreo interno que exponga en tiempo real:

- Estado del bot (modo, versión, health de servicios)
- Posiciones abiertas con PnL en vivo y métricas de trailing/break-even
- Historial de decisiones del Decision Aggregator (señales, confianza, acción tomada)
- Feed de ticks ATR en vivo (introducido en F14 — loop de ticks en `PositionManager`)
- Órdenes activas e historial de ejecuciones
- Métricas de riesgo y alertas del Risk Engine
- Resultados de backtests

El proyecto es 100% Python (FastAPI + PostgreSQL). No hay experiencia previa en JavaScript en el equipo. El deploy es Docker Compose con tres servicios: `postgres`, `app` y `worker`.

Las tres opciones candidatas planteadas son:

1. **React + Vite** — SPA compilada a archivos estáticos
2. **Next.js** — React con SSR/SSG más servidor Node.js
3. **Jinja (server-rendered)** — Templates HTML servidos desde FastAPI

---

## Decisión

**React + Vite**, con el build (`dist/`) servido como `StaticFiles` desde el mismo container de FastAPI.

No se agrega ningún servicio nuevo al Docker Compose. El frontend vive en `/frontend` en el repo y se compila en tiempo de build de la imagen, o como paso de CI previo al deploy.

---

## Justificación

### Por qué React + Vite gana sobre Jinja

Jinja requiere JavaScript de todas formas para lo que más importa en este dashboard:

- Gráficos de precio y ATR en tiempo real → TradingView Lightweight Charts o Recharts (ambos React)
- Actualizaciones por WebSocket/SSE del feed de ticks → manipulación del DOM desde JS ad-hoc
- Estado reactivo (posición cambia, PnL se actualiza) → sin framework, código espagueti

Con Jinja el resultado final sería HTML + Jinja + JS ad-hoc mezclados. React + Vite da el mismo resultado con un modelo de componentes coherente y mayor ecosistema de charting.

### Por qué React + Vite gana sobre Next.js

Next.js exige un servidor Node.js en producción, lo que implica:

- Cuarto servicio en Docker Compose (`dashboard`)
- Mantenimiento de runtime Node.js adicional (versiones, actualizaciones)
- Hidratación SSR sin beneficio: el dashboard es una herramienta interna autenticada (sin SEO, sin social sharing, sin crawlers)
- Más complejidad de deploy para un caso de uso que no lo justifica

React + Vite produce un `dist/` estático que FastAPI sirve con `app.mount("/", StaticFiles(...))`. Sin servidor extra.

### Costo de curva de aprendizaje

El equipo es Python-first. Agregar React suma carga cognitiva, pero:

- TypeScript + React es hoy el estándar de facto para UIs reactivas; la documentación y el tooling son maduros
- Vite tiene configuración casi nula comparada con webpack/CRA
- La alternativa (Jinja + JS manual) implica aprender JS de igual forma pero sin estructura

El trade-off es aceptable dado que el dashboard es una épica discreta (F15) con alcance acotado.

---

## Consecuencias

### Estructura de archivos

```
/frontend          ← nuevo directorio (fuera de /backend)
  /src
    /components
    /hooks
    /pages
  package.json
  vite.config.ts
  tsconfig.json
```

### Desviación respecto de la spec (Fase 1, §4.11)

La sección 4.11 de la spec ("Estructura de carpetas propuesta") lista `dashboard/` con `package.json`, `next.config.js` y `src/` — un placeholder anterior a esta decisión, creado en el commit inicial de estructura del repo (antes de evaluar el stack). Esta ADR lo reemplaza: el directorio real es `/frontend`, no `dashboard/`, y el stack es React + Vite, no Next.js, por las razones detalladas en Justificación. Los placeholders vacíos de `dashboard/` (`package.json`, `next.config.js`) fueron eliminados al crear el scaffold real en `/frontend`.

### Integración con FastAPI

En `backend/app/main.py`, **todos los routers de API y WebSocket deben registrarse antes del mount del SPA**. El mount es un catch-all: cualquier ruta registrada después queda inaccesible.

```python
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# 1. Registrar routers PRIMERO (API REST + WebSocket)
app.include_router(api_router, prefix="/api")
app.include_router(ws_router, prefix="/api/ws")  # WebSocket bajo /api/ws

# 2. Assets estáticos del build (JS/CSS/imágenes) bajo /assets
app.mount("/assets", StaticFiles(directory="frontend/dist/assets"), name="frontend-assets")

# 3. Catch-all SPA fallback — necesario porque StaticFiles(html=True) sólo
# sirve index.html en la raíz del mount, no en paths anidados. Sin esto,
# un refresh o deep-link a una ruta de React Router (ej. /positions,
# /backtests/123) devuelve 404 en vez del index.html de la SPA.
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str) -> FileResponse:
    return FileResponse("frontend/dist/index.html")
```

Convenio de prefijos:

| Tipo | Prefijo |
|---|---|
| REST API | `/api/...` |
| WebSocket / SSE | `/api/ws/...` |
| Assets estáticos (JS/CSS) | `/assets/...` |
| SPA (React, catch-all) | `/{full_path:path}` (registrado último) |

### Autenticación y autorización

Fuera de scope de F15. El dashboard expone datos sensibles (posiciones abiertas, PnL en vivo, decisiones del Aggregator, métricas de riesgo) y **no debe quedar accesible sin autenticación** antes de ir a producción.

Se crea una tarjeta separada para definir el mecanismo de auth (comparte sesión/token con la API existente vs. login propio) antes de habilitar el dashboard fuera de un entorno local/interno.

### Docker

El `Dockerfile` existente agrega un paso de build del frontend:

```dockerfile
# Build frontend
FROM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# Backend image
FROM python:3.12-slim
...
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
```

`npm ci` requiere `package-lock.json` versionado en el repo (a diferencia de `npm install`, no lo genera). Quien implemente F15 debe commitear el lockfile junto con `package.json` al crear `/frontend`, o el build de Docker falla.

No se modifica `docker-compose.yml`.

### Lo que NO cambia

- El stack de backend (Python, FastAPI, PostgreSQL) no cambia.
- Los tres servicios de Docker Compose no cambian.
- Ningún endpoint de API existente cambia.

---

## Alternativas descartadas

**Jinja (server-rendered)**: Viable solo para dashboards sin datos en tiempo real. Para este caso requeriría JS ad-hoc igual, pero sin el modelo de componentes de React. Descartado por experiencia de desarrollo inferior sin reducción real de complejidad.

**Next.js**: Agrega un servidor Node.js que no aporta beneficio medible para un dashboard interno. SSR/SSG son irrelevantes sin SEO ni cold-start. Descartado por complejidad operacional injustificada.
