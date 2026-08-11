# Dashboard frontend (-10k)

SPA en React + Vite + TypeScript para la vista de estado del bot y el kill switch manual. Ver `docs/decisions/F15-01-frontend-stack.md` para el contexto de la elección del stack.

## Desarrollo

```bash
npm install
npm run dev
```

`vite.config.ts` proxea `/api` a `http://localhost:8000`, así que hace falta el backend corriendo en ese puerto (`uv run uvicorn backend.app.main:app --reload`).

## Scripts

- `npm run dev` — servidor de desarrollo con HMR.
- `npm run build` — type-check (`tsc -b`) + build de producción a `dist/`.
- `npm run lint` — oxlint.
- `npm run test` — tests unitarios (vitest).
- `npm run preview` — sirve el build de `dist/` localmente.

## Build de producción

El build se sirve como estáticos desde FastAPI (`backend/app/main.py` monta `frontend/dist` si existe). En Docker, el stage `frontend-build` de `infra/Dockerfile` corre `npm ci && npm run build` antes de armar la imagen final.
