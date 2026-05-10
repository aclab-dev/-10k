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

## Reglas no negociables

- Margen máximo por operación: **10 USDT**
- Pares: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT
- Margin **ISOLATED** obligatorio
- Sin SL → no se opera

## Documentación

- Spec maestra: `PDF_01_Especificacion_y_Arquitectura_Final.pdf`
- Board Trello: https://trello.com/b/TDbywbhP/10k
