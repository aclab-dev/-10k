# F10-05 — Idempotencia de Órdenes

**Estado**: Aceptada  
**Fecha**: 2026-06-09  
**Epic**: F10 — Execution Engine  

---

## Contexto

El Execution Engine debe garantizar que una orden no se envíe dos veces al exchange ante crashes, timeouts, reinicios o reintentos en el ciclo de bot.

Estado al momento de esta decisión:

- `IdempotencyConfig` existe en `config.yaml` con `retry_policy: CHECK_EXISTING_ORDER_BEFORE_RETRY` y flags `require_cycle_id / require_decision_id / require_trade_plan_id / require_client_order_id` todos en `true`.
- `OrderRequest.client_order_id` existe como campo UUID en el schema del adapter.
- `PaperAdapter.place_order()` ya implementa idempotencia a nivel memoria (devuelve el resultado previo si el `client_order_id` ya existe en `self._orders`).
- La tabla `orders` en la DB **no tiene columna `client_order_id`**: no hay lookup persistente posible ante crash + restart.
- `execution/engine.py` está vacío — el ExecutionEngine aún no está implementado.

---

## Decisión

### 1. Clave de idempotencia: `client_order_id`

El `client_order_id` es un **UUID4 aleatorio** generado por el ExecutionEngine antes de construir el `OrderRequest`. No se deriva de otros IDs — su aleatoriedad evita colisiones si el mismo trade plan se reintenta con parámetros distintos.

El `client_order_id` se persiste en `orders.client_order_id` (columna UNIQUE NOT NULL) **antes** de llamar a `adapter.place_order()`. Este orden es deliberado: si el bot crashea después de persistir pero antes de enviar, el retry detecta el registro y reenvía el mismo `client_order_id`; el exchange o el adapter lo trata como idempotente.

### 2. Mapeo de conceptos del config

| Config field | Mapeo en el modelo de datos |
|---|---|
| `require_cycle_id` | `bot_run_id` — el `BotRun` es el ciclo. No se crea un modelo `Cycle` separado. |
| `require_decision_id` | `decision_id` del `ModelDecision` que originó el trade, referenciado via `Trade.risk_validation_id → RiskValidation.decision_aggregation_id → DecisionAggregation.decision_id`. |
| `require_trade_plan_id` | `trade_id` — el `Trade` es el plan de ejecución. Las órdenes viven bajo un Trade. |
| `require_client_order_id` | `orders.client_order_id` — columna nueva, obligatoria. |

### 3. Flujo de ejecución con idempotencia

```
ExecutionEngine.place_order(trade, order_params):
  1. Generar client_order_id = uuid4()
  2. Persistir Order en DB (status=PENDING, client_order_id=client_order_id)
  3. Llamar adapter.place_order(OrderRequest(client_order_id=..., ...))
  4. Actualizar Order en DB con el resultado (status, fill_price, etc.)
```

Ante retry (crash entre paso 2 y 3, o entre 3 y 4):

```
ExecutionEngine.retry_or_recover(trade):
  1. Buscar en DB: SELECT * FROM orders WHERE trade_id=? AND status=PENDING
  2. Si existe → reenviar el MISMO client_order_id al adapter
     → adapter retorna el resultado previo (idempotente) sin duplicar
  3. Si no existe → ejecutar el flujo normal desde el paso 1
```

### 4. Retry policy: `CHECK_EXISTING_ORDER_BEFORE_RETRY`

Antes de cualquier reintento el ExecutionEngine consulta `orders` por `trade_id + status`. Si ya hay una orden PENDING o FILLED para ese trade, no se genera un nuevo `client_order_id` — se reutiliza el existente. Esto previene el doble-fill más peligroso: FILLED en el exchange pero PENDING en la DB (crash post-fill, pre-update).

### 5. Defensa en capas

| Capa | Mecanismo | Cobertura |
|---|---|---|
| DB (primaria) | `orders.client_order_id` UNIQUE + lookup pre-retry | Crash entre ciclos, restarts |
| Adapter (secundaria) | Dict en memoria (PaperAdapter) / `client_order_id` en exchange API | Llamadas duplicadas en el mismo proceso |
| Exchange (terciaria) | BingX acepta `clientOrderId` nativo | Red flaky, reintentos HTTP |

La capa primaria (DB) es la única que sobrevive reinicios. Las capas secundaria y terciaria son defensa en profundidad.

---

## Consecuencias

### Cambios de datos

- **`orders` table**: agregar columna `client_order_id VARCHAR(36) NOT NULL UNIQUE` + constraint único.
- **`Order` model (SQLAlchemy)**: agregar campo `client_order_id: Mapped[str]`.
- Nueva migración Alembic para la columna.

### Cambios de código (futuros, en F10)

- El ExecutionEngine debe generar y persistir `client_order_id` antes de llamar al adapter.
- La lógica de retry del ExecutionEngine consulta `orders` por `trade_id` antes de re-emitir.
- Los adapters reales (BingXAdapter) deben propagar `client_order_id` como `clientOrderId` en la API de BingX.

### Lo que NO cambia

- `OrderRequest.client_order_id` sigue siendo UUID4 — el schema del adapter no cambia.
- `PaperAdapter` no cambia — ya implementa la idempotencia correctamente.
- `IdempotencyConfig` ya tiene los valores correctos en `config.yaml`.

---

## Alternativas descartadas

**UUID derivado de decision_id + símbolo**: predecible pero complica el rehash si el mismo decision genera múltiples órdenes (entry + SL + TP son 3 órdenes distintas para el mismo trade). UUID4 independiente es más simple y correcto.

**Sin columna en DB, solo memory**: no sobrevive reinicios. Descartado por los requisitos de `retry_policy`.
