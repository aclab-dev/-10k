# F15-01 — Stack Frontend: Dashboard

**Estado**: Aceptada  
**Fecha**: 2026-07-27  
**Epic**: F15 — Dashboard  

---

## Contexto

El bot necesita un dashboard de monitoreo interno que exponga en tiempo real:

- Estado del bot (modo, versión, health de servicios)
- Posiciones abiertas con PnL en vivo y métricas de trailing/break-even
- Historial de decisiones del Decision Aggregator (señales, confianza, acción tomada)
- Feed de ticks ATR en vivo (introducido en F14 — `atr_feed_unavailable`)
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

### Integración con FastAPI

En `backend/app/main.py`, después de registrar los routers de API:

```python
from fastapi.staticfiles import StaticFiles

app.mount("/", StaticFiles(directory="frontend/dist", html=True), name="frontend")
```

Los endpoints de API quedan bajo `/api/...` para no colisionar con las rutas del SPA.

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

No se modifica `docker-compose.yml`.

### Lo que NO cambia

- El stack de backend (Python, FastAPI, PostgreSQL) no cambia.
- Los tres servicios de Docker Compose no cambian.
- Ningún endpoint de API existente cambia.

---

## Alternativas descartadas

**Jinja (server-rendered)**: Viable solo para dashboards sin datos en tiempo real. Para este caso requeriría JS ad-hoc igual, pero sin el modelo de componentes de React. Descartado por experiencia de desarrollo inferior sin reducción real de complejidad.

**Next.js**: Agrega un servidor Node.js que no aporta beneficio medible para un dashboard interno. SSR/SSG son irrelevantes sin SEO ni cold-start. Descartado por complejidad operacional injustificada.
