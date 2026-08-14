# -10k

Bot autónomo de futuros crypto con GPT + Quant Signals + Risk Engine determinístico.

## Modo del sistema

`AUTONOMOUS_FUTURES_GPT55_QUANT_CONTROLLED_RISK`

## Filosofía

- **PAPER-first, LIVE bloqueado** hasta completar backtesting, replay histórico y checklist LIVE.
- Edge desde módulos cuantitativos reproducibles.
- GPT como evaluador contextual — nunca autoridad final.
- Risk Engine determinístico tiene la última palabra.

## Stack

- Python 3.12+
- FastAPI
- PostgreSQL + SQLAlchemy + Alembic
- Docker Compose

## Entornos

| Entorno | Variable | Condición de habilitación |
|---|---|---|
| Paper | `ENVIRONMENT=PAPER` | Disponible desde F2 |
| Testnet | `ENVIRONMENT=TESTNET` | Backtesting aprobado |
| Live | `ENVIRONMENT=LIVE` + `I_UNDERSTAND_LIVE_RISK=YES` | Checklist LIVE completo |

## Dashboard — auth

Los endpoints del dashboard (`/api/status`, `/api/decisions`, `/api/risk`, `/api/tokens`)
exigen un bearer token. `/health` queda público para el healthcheck de Docker.

Antes del primer `docker compose up`, generar las credenciales:

```bash
python scripts/hash_password.py
```

El script imprime `DASHBOARD_PASSWORD_HASH` y `DASHBOARD_SECRET_KEY` para pegar en
`.env`, junto con `DASHBOARD_USERNAME`. **La app no levanta si falta alguna de las
tres** (fail-closed). Para desarrollo local sin auth: `BOT__DASHBOARD_AUTH__ENABLED=false`.

`DASHBOARD_PASSWORD_HASH` sale del script con cada `$` ya escapado como `$$`
(pegalo tal cual): Docker Compose interpola `.env` y sin ese escape corrompe el
hash, dejando `cryptobot-app` en crash-loop. Ver el comentario junto a la
variable en `.env.example` para el detalle.

Obtener un token:

```bash
curl -sX POST localhost:8000/api/auth/login -H 'Content-Type: application/json' -d '{"username":"admin","password":"..."}'
```

## Reglas no negociables

- Margen máximo por operación: **10 USDT**
- Pares: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT
- Margin **ISOLATED** obligatorio
- Sin SL → no se opera

## Documentación

- Spec maestra: `PDF_01_Especificacion_y_Arquitectura_Final.pdf`
- Board Trello: https://trello.com/b/TDbywbhP/10k
