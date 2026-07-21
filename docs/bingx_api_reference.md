# BingX Futures API — Referencia

> Documentación de la API oficial de BingX Perpetual Futures (USDT-M) relevante para F13.
> Fuentes: [BingX API Docs](https://bingx-api.github.io/docs/#/en-us/swapV2/), SDKs oficiales, headers de respuesta HTTP real.

---

## 1. URLs base

| Tipo         | URL                                            |
|--------------|------------------------------------------------|
| REST (prod)  | `https://open-api.bingx.com`                   |
| WebSocket    | `wss://open-api-swap.bingx.com/swap-market`    |

No existe un entorno testnet/sandbox dedicado con URL separada (ver sección [Sandbox](#6-sandbox)).

---

## 2. Autenticación

### Headers requeridos

```
X-BX-APIKEY: <api_key>
```

### Firma (HMAC-SHA256)

Todos los endpoints privados requieren firma. El proceso es:

1. Construir el query string con todos los parámetros, incluyendo `timestamp` (Unix ms).
2. Calcular `signature = HMAC-SHA256(secret_key, query_string).hexdigest()`.
3. Agregar `&signature=<hex>` al final del query string.

```python
import hashlib, hmac, time

params = f"symbol=BTC-USDT&timestamp={int(time.time() * 1000)}"
signature = hmac.new(secret_key.encode(), params.encode(), hashlib.sha256).hexdigest()
url = f"https://open-api.bingx.com/openApi/swap/v2/user/balance?{params}&signature={signature}"
```

### Parámetros de timing

| Param       | Descripción                                         | Requerido |
|-------------|-----------------------------------------------------|-----------|
| `timestamp` | Unix timestamp en milisegundos                      | Sí (auth) |
| `recvWindow`| Ventana de validez en ms (default implícito: 5000)  | No        |

Los endpoints públicos de market data **no requieren** firma ni API key.

---

## 3. Rate Limits

Confirmado desde headers de respuesta HTTP real:

```
x-ratelimit-requests-remain: 499
x-ratelimit-requests-expire: 10000
```

| Límite              | Valor                        |
|---------------------|------------------------------|
| Requests por ventana | 500                         |
| Duración de ventana | 10 segundos (10,000 ms)      |
| Throughput efectivo | ~50 req/s                    |
| Scope               | Por IP (endpoints públicos)  |

> **Importante:** Los 500 req/10s están confirmados únicamente desde headers de endpoints públicos de market data.
> Los endpoints de `/trade/*` pueden tener límites más restrictivos por cuenta (no confirmado en la fase F13).
> Al implementar el throttler en LIVE, medir los headers contra `/trade/order` antes de fijar el límite.
> Superar el límite retorna HTTP 429.

### Campos en la respuesta
Los endpoints de market data incluyen en el response los headers:
- `x-ratelimit-requests-remain` — requests restantes en la ventana actual
- `x-ratelimit-requests-expire` — ms hasta que se reinicia la ventana

---

## 4. Formato de respuesta

Todos los endpoints retornan JSON con la estructura:

```json
{
  "code": 0,
  "msg": "",
  "data": { ... }
}
```

- `code == 0` → éxito
- `code != 0` → error (revisar `msg`)

### Ejemplo — Contrato BTC-USDT

```json
{
  "contractId": "100",
  "symbol": "BTC-USDT",
  "size": "0.0001",
  "quantityPrecision": 4,
  "pricePrecision": 1,
  "makerFeeRate": 0.0002,
  "takerFeeRate": 0.0005,
  "tradeMinQuantity": 0.0001,
  "tradeMinUSDT": 2,
  "currency": "USDT",
  "asset": "BTC",
  "status": 1
}
```

- Symbol format: `<ASSET>-USDT` (ej: `BTC-USDT`, `ETH-USDT`)
- Todos los contratos son USDT-margined (USDT-M)

---

## 5. Endpoints REST — Perpetual Swap V2

### 5.1 Market Data (públicos — sin auth)

| Endpoint                               | Método | Descripción                      |
|----------------------------------------|--------|----------------------------------|
| `/openApi/swap/v2/quote/contracts`     | GET    | Info de todos los contratos      |
| `/openApi/swap/v2/quote/price`         | GET    | Precio último (`symbol?`)        |
| `/openApi/swap/v2/quote/depth`         | GET    | Book de órdenes (`symbol, limit`)|
| `/openApi/swap/v2/quote/trades`        | GET    | Últimas trades (`symbol, limit`) |
| `/openApi/swap/v2/quote/premiumIndex`  | GET    | Funding rate actual (`symbol?`)  |
| `/openApi/swap/v2/quote/fundingRate`   | GET    | Historial funding rate           |
| `/openApi/swap/v2/quote/klines`        | GET    | Velas OHLCV (`symbol, interval`) |
| `/openApi/swap/v2/quote/openInterest`  | GET    | Open interest (`symbol`)         |
| `/openApi/swap/v2/quote/ticker`        | GET    | Ticker 24h (`symbol?`)           |

**Intervalos de velas disponibles:**
`1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M`

**Límite de velas por request:** default 500, máximo 1440. Paginar con `startTime`/`endTime` para histórico.

### 5.2 Account (requieren auth)

| Endpoint                             | Método | Descripción                         |
|--------------------------------------|--------|-------------------------------------|
| `/openApi/swap/v2/user/balance`      | GET    | Balance de cuenta (USDT disponible) |
| `/openApi/swap/v2/user/positions`    | GET    | Posiciones abiertas (`symbol`)      |
| `/openApi/swap/v2/user/income`       | GET    | Historial PnL y flujo de fondos     |

### 5.3 Trade (requieren auth)

| Endpoint                                      | Método | Descripción                          |
|-----------------------------------------------|--------|--------------------------------------|
| `/openApi/swap/v2/trade/order`                | POST   | Colocar orden                        |
| `/openApi/swap/v2/trade/order/test`           | POST   | Orden de prueba (sin ejecución real) |
| `/openApi/swap/v2/trade/batchOrders`          | POST   | Órdenes en batch                     |
| `/openApi/swap/v2/trade/closeAllPositions`    | POST   | Cerrar todas las posiciones          |
| `/openApi/swap/v2/trade/order`                | DELETE | Cancelar orden (`orderId, symbol`)   |
| `/openApi/swap/v2/trade/batchOrders`          | DELETE | Cancelar batch de órdenes            |
| `/openApi/swap/v2/trade/allOpenOrders`        | DELETE | Cancelar todas las órdenes           |
| `/openApi/swap/v2/trade/openOrders`           | GET    | Órdenes abiertas actuales            |
| `/openApi/swap/v2/trade/order`                | GET    | Consultar orden por ID               |
| `/openApi/swap/v2/trade/allOrders`            | GET    | Historial de órdenes                 |
| `/openApi/swap/v2/trade/forceOrders`          | GET    | Órdenes de liquidación forzada       |
| `/openApi/swap/v2/trade/leverage`             | GET    | Consultar leverage actual            |
| `/openApi/swap/v2/trade/leverage`             | POST   | Cambiar leverage (`symbol, side, leverage`) |
| `/openApi/swap/v2/trade/marginType`           | GET    | Consultar modo de margen actual      |
| `/openApi/swap/v2/trade/marginType`           | POST   | Cambiar modo de margen (`marginType`) |
| `/openApi/swap/v2/trade/positionMargin`       | POST   | Ajustar margen aislado               |
| `/openApi/swap/v1/positionSide/dual`          | GET    | Consultar modo de posición (ONE_WAY / hedge) — ver nota §5.4 |
| `/openApi/swap/v1/positionSide/dual`          | POST   | Cambiar modo de posición (`dualSidePosition`) — ver nota §5.4 |

> **Regla no negociable del proyecto — Margin Type:** el adapter debe verificar y forzar `marginType=ISOLATED` al inicializar la conexión.
> `CROSS` está prohibido. Igual que con el modo de posición, este chequeo debe ocurrir antes de colocar cualquier orden.

**Parámetros clave de orden:**

| Param          | Tipo    | Descripción                                           |
|----------------|---------|-------------------------------------------------------|
| `symbol`       | string  | Ej: `BTC-USDT`                                        |
| `type`         | string  | `MARKET`, `LIMIT`, `STOP`, `STOP_MARKET`, `TAKE_PROFIT`, `TAKE_PROFIT_MARKET` |
| `side`         | string  | `BUY` o `SELL`                                        |
| `positionSide` | string  | `BOTH` (ONE_WAY — modo del proyecto) / `LONG` o `SHORT` (hedge mode, no usado) |
| `quantity`     | float   | Cantidad del activo base                              |
| `price`        | float   | Precio (para órdenes LIMIT)                           |
| `stopPrice`    | float   | Precio de trigger (para SL/TP)                        |
| `reduceOnly`   | bool    | Solo reducir posición existente                       |

### 5.4 Position Mode (ONE_WAY vs Hedge)

> **Regla no negociable del proyecto:** el adapter debe operar en modo **ONE_WAY** (`dualSidePosition=false`).
> En ONE_WAY todas las órdenes usan `positionSide=BOTH`. Nunca usar `LONG`/`SHORT` directamente.

| Endpoint                                       | Método | Descripción                                    |
|------------------------------------------------|--------|------------------------------------------------|
| `/openApi/swap/v1/positionSide/dual`           | GET    | Consultar modo actual (`dualSidePosition`)     |
| `/openApi/swap/v1/positionSide/dual`           | POST   | Cambiar modo (`dualSidePosition=true/false`)   |

> **Corrección (tarjeta [101])**: la investigación original documentó este endpoint
> como `/openApi/swap/v2/trade/positionSide/dual`. Contra la cuenta demo real, BingX
> rechaza ese path con `code 100400: this api is not exist`. Confirmado contra la
> implementación de [ccxt](https://github.com/ccxt/ccxt/blob/master/python/ccxt/bingx.py)
> que el endpoint real vive bajo `/swap/v1/` (no `/v2/trade/`), sin segmento `/trade/`.
> El adapter (`bingx_adapter.py::_ensure_one_way_mode`) ya usa el path corregido.

**Parámetro:**

| Param               | Tipo | Descripción                                         |
|---------------------|------|-----------------------------------------------------|
| `dualSidePosition`  | bool | `false` = ONE_WAY (requerido por el proyecto)       |

**El adapter debe verificar y forzar `dualSidePosition=false` al inicializar la conexión.**
Respuesta GET:

```json
{ "code": 0, "data": { "dualSidePosition": false } }
```

### 5.5 Listen Key (para WebSocket privado)

| Endpoint                              | Método | Descripción                          |
|---------------------------------------|--------|--------------------------------------|
| `/openApi/user/auth/userDataStream`   | POST   | Crear listen key                     |
| `/openApi/user/auth/userDataStream`   | PUT    | Extender listen key (requiere `listenKey` en query) |
| `/openApi/user/auth/userDataStream`   | DELETE | Eliminar listen key                  |

El listen key tiene un timeout de **60 minutos**. Extender con PUT antes de que venza — se recomienda hacerlo cada ~50 min para tener margen de seguridad (ver nota 6, sección 9).

---

## 6. WebSocket

### Cuándo usar REST vs WebSocket

| Caso de uso                              | Recomendado |
|------------------------------------------|-------------|
| Ejecutar órdenes                         | REST        |
| Consultar estado de orden / posición     | REST        |
| Gestión de cuenta (balance, leverage)    | REST        |
| Precio en tiempo real (ticker, markPrice)| WebSocket   |
| Order book en tiempo real                | WebSocket   |
| Velas (klines) en tiempo real            | WebSocket   |
| Actualizaciones de cuenta/posición       | WebSocket (user data) |
| Historial / datos batch                  | REST        |

**Regla práctica:** REST para operaciones, WebSocket para feeds de datos que se actualizan continuamente.

### Conexión

```
wss://open-api-swap.bingx.com/swap-market
# Con user data:
wss://open-api-swap.bingx.com/swap-market?listenKey=<key>
```

### Formato de suscripción

```json
{
  "id": "uuid-cualquiera",
  "reqType": "sub",
  "dataType": "<stream_name>"
}
```

### Streams disponibles — Market

| Stream                         | Descripción                       |
|--------------------------------|-----------------------------------|
| `<symbol>@markPrice`           | Mark price en tiempo real         |
| `<symbol>@lastPrice`           | Último precio                     |
| `<symbol>@kline_<interval>`    | Vela en tiempo real               |
| `<symbol>@depth<N>`            | Order book (N niveles)            |
| `<symbol>@trade`               | Trades recientes                  |

Ejemplo de stream name: `BTC-USDT@kline_1m`, `ETH-USDT@markPrice`

### Keep-alive del WebSocket (Ping / Pong)

BingX envía un **ping frame** cada ~20 segundos. El cliente debe responder con un **pong frame** dentro de ese mismo intervalo o la conexión se cierra.

Adicionalmente, el protocolo BingX acepta pings a nivel de aplicación:

```json
{ "ping": 1234567890123 }
```

Respuesta esperada:

```json
{ "pong": 1234567890123 }
```

**Para el adapter:** implementar un loop de pong automático (responder ping frames del servidor) y opcionalmente enviar pings de aplicación cada 15-20s para detectar desconexiones silenciosas. Sin esto habrá reconexiones inesperadas en producción.

### Streams disponibles — User Data (requiere listen key)

| Evento                          | Descripción                           |
|---------------------------------|---------------------------------------|
| `ACCOUNT_UPDATE`                | Cambios en balance / posiciones       |
| `ORDER_TRADE_UPDATE`            | Actualizaciones de órdenes            |

---

## 7. Sandbox

> **Corrección (tarjeta [101])**: la investigación original afirmaba que BingX no
> tiene un host de testnet separado. Es **incorrecto**. BingX expone un host de
> demo/sandbox (VST — Virtual Simulated Trading) con fondos virtuales propios:
>
> | Entorno | Host |
> |---------|------|
> | Producción (LIVE)      | `https://open-api.bingx.com`     |
> | Demo/sandbox (VST)     | `https://open-api-vst.bingx.com` |
>
> Confirmado contra la implementación de [ccxt](https://github.com/ccxt/ccxt/blob/master/python/ccxt/bingx.py)
> (`urls['api']` vs `urls['test']`). El adapter selecciona el host por `Environment`:
> `LIVE` → producción, `TESTNET`/`PAPER` → VST. Las credenciales de la cuenta demo
> se generan desde la misma API Management, pero su balance/posiciones viven en el
> host VST — pegarle al host de producción con esas credenciales devuelve una cuenta
> vacía (balance 0), no un error.

Opciones disponibles para testing:

### 7.1 Test order endpoint

```
POST /openApi/swap/v2/trade/order/test
```

Acepta los mismos parámetros que una orden real pero **no ejecuta** la orden. Útil para validar que la firma y los parámetros son correctos.

### 7.2 PAPER mode (enfoque del proyecto)

El sistema -10k implementa su propio PAPER mode (F10) que simula la ejecución de órdenes internamente sin conectarse al exchange real. Este es el enfoque principal para testing antes de pasar a LIVE.

### 7.3 Cuentas demo

BingX ofrece cuentas demo directamente en su plataforma web (https://bingx.com), con **fondos virtuales**. El acceso por API es vía el host VST (ver corrección al inicio de esta sección): `https://open-api-vst.bingx.com`. No confundir el balance de la cuenta demo (host VST) con el de producción (host normal).

---

## 8. Fees

Extraído de los datos reales de la API de contratos:

| Fee type    | Valor default | Notas                                 |
|-------------|---------------|---------------------------------------|
| Maker fee   | 0.02% (0.0002)| Órdenes que agregan liquidez (LIMIT)  |
| Taker fee   | 0.05% (0.0005)| Órdenes que consumen liquidez (MARKET)|

Los valores exactos varían por par y nivel VIP de la cuenta. Confirmar con `/openApi/swap/v2/quote/contracts`.

---

## 9. Notas para el adaptador (F13)

1. **Host por entorno** — el adapter selecciona el host según `Environment`: `LIVE` → `open-api.bingx.com`, `TESTNET`/`PAPER` → `open-api-vst.bingx.com` (VST/demo, fondos virtuales). Ver sección 7.
2. **Firma HMAC-SHA256 hexdigest** — distinta a la firma base64 usada en la V1 legacy.
3. **Symbol format** es `BTC-USDT` (con guión), no `BTCUSDT` como en Binance.
4. **Modos obligatorios al inicializar** — el adapter debe verificar y forzar dos configuraciones antes de operar: (a) `dualSidePosition=false` (ONE_WAY, ver sección 5.4) — todas las órdenes usan `positionSide=BOTH`; (b) `marginType=ISOLATED` — `CROSS` está prohibido (ver nota en sección 5.3).
5. **Rate limit** — 500 req / 10s confirmado para market data. Los endpoints de `/trade/*` pueden tener límites distintos; medir en LIVE antes de fijar el throttler (ver sección 3).
6. **WebSocket keep-alive** — dos niveles: (a) responder ping frames del servidor (ver sección Keep-alive), (b) extender el listen key con PUT cada ~50 min — el timeout real es 60 min, renovar antes evita expiración silenciosa del stream (ver sección 5.5).
7. **Minimum order** — `tradeMinUSDT: 2 USDT`. Validar antes de enviar.
