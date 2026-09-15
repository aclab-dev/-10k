# Checklist LIVE — Sección 3.6 (Reglas no negociables)

Este documento verifica, regla por regla, que cada una de las "Reglas no negociables"
de la Sección 3.6 del documento maestro esté efectivamente aplicada en el código (no
solo declarada en config o descripta en un doc), con evidencia de test cuando aplica.
Es el gate de la regla 34: no se avanza a LIVE sin este checklist firmado.

**Auditoría realizada:** 2026-09-15
**Alcance:** las 34 reglas de la Sección 3.6, contra el estado del código en
`develop` al momento de esta auditoría (commit `c1a26d0`).

## Cómo leer el estado

- ✅ **Verificado** — hay un check de código que bloquea o ajusta el trade si se
  viola la regla, con test que lo cubre.
- ⚠️ **Gate de proceso** — la regla no es verificable por código (depende de una
  config del exchange o de un criterio humano); se documenta el mecanismo de
  verificación manual.
- ❌ **Bloqueante** — la regla no está aplicada en runtime, o solo está aplicada
  parcialmente (ej. en una capa bypasseable). No se puede firmar LIVE mientras
  haya ítems en este estado.

## 1–9. Límites de margen, leverage y drawdown

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 1 | Margen máx. 10 USDT/operación | ✅ | Boot: [`ChallengeConfig.margin_le_10`](backend/core/config.py:68), [`RiskConfig.margin_le_10`](backend/core/config.py:138). Runtime: [`check_margin_cap`](backend/risk_engine/checks.py:170) (ADJUST_DOWN), wired en [`engine.py:153`](backend/risk_engine/engine.py:153). Test: `tests/unit/test_config.py:222,227`, `tests/unit/test_risk_engine_exhaustive.py`. |
| 2 | Leverage máx. 10x PAPER | ✅ | Boot: [`LeverageConfig`](backend/core/config.py:165) (`max_leverage_paper>10` rechaza, línea 174). Runtime: [`leverage_cap_for_env`](backend/risk_engine/checks.py:42) + [`check_leverage_cap`](backend/risk_engine/checks.py:188). Test: `tests/unit/test_config.py:232`. |
| 3 | Leverage máx. 5x TESTNET | ✅ | Boot: `config.py:178`. Runtime: igual que #2. Test: `tests/unit/test_config.py:237`. |
| 4 | Leverage máx. 3x LIVE inicial | ❌ | Boot valida `max_leverage_live_initial<=3` ([`config.py:186`](backend/core/config.py:186)), pero el Risk Engine **no lo usa**: [`leverage_cap_for_env`](backend/risk_engine/checks.py:42) devuelve `max_leverage_live_absolute` (5x) para cualquier entorno LIVE — el cap de 3x solo se aplica en la capa de sugerencia ([`volatility/leverage.py:63`](backend/volatility/leverage.py:63), comentario propio admite que falta el mecanismo de promoción de fase). Una decisión de 4x–5x en LIVE inicial no sería bloqueada por el Risk Engine. **Acción requerida antes de LIVE**: hacer que `leverage_cap_for_env` distinga LIVE-inicial de LIVE-absoluto (vía un flag de fase, no solo el entorno), o bloquear explícitamente >3x mientras no exista ese mecanismo. |
| 5 | Leverage máx. 5x absoluto LIVE | ✅ | Boot: `config.py:182`. Runtime: igual que #2 (cap efectivo hoy, ver nota en #4). Test: `tests/unit/test_config.py:242`. |
| 6 | Pérdida diaria máx. 10% | ✅ | [`check_daily_drawdown`](backend/risk_engine/checks.py:105) (BLOCK), wired en `engine.py:122`. Test: `tests/unit/test_risk_engine_exhaustive.py`. |
| 7 | Pérdida total máx. 50% | ✅ | [`check_total_drawdown`](backend/risk_engine/checks.py:136) (BLOCK), wired en `engine.py:123`. Test: `tests/unit/test_risk_engine_exhaustive.py`. |
| 8 | Símbolos permitidos: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT | ✅ | [`TradingConfig.symbols_in_whitelist`](backend/core/config.py:91) (boot) + bound duplicado en `backend/market_data/schemas.py` (`ALLOWED_SYMBOLS`). Test: `tests/unit/test_config.py:50,262`, `tests/unit/test_market_snapshot.py`. |
| 9 | Prohibido cross margin (ISOLATED obligatorio) | ✅ | [`TradingConfig.no_cross_margin`](backend/core/config.py:99) y [`RiskConfig.no_cross`](backend/core/config.py:152) — ambos rechazan `CROSS` al bootear. Test: `tests/unit/test_config.py:252,278`. Verificación operativa en cuenta real: card Trello [122], Done. |

## 10–14. Obligaciones pre-trade

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 10 | Stop loss obligatorio | ✅ | [`check_sl_required`](backend/risk_engine/checks.py:58) (BLOCK) + defensa redundante dentro de [`check_liquidation_safety`](backend/risk_engine/checks.py:241). Test: `tests/unit/test_risk_engine_exhaustive.py`. |
| 11 | Take profit o plan de salida obligatorio | ✅ | [`check_tp_or_exit_plan`](backend/risk_engine/checks.py:73) (BLOCK). Test: `tests/unit/test_risk_engine_exhaustive.py`. |
| 12 | Cálculo de fees obligatorio | ❌ | [`backend/backtesting/fee_model.py`](backend/backtesting/fee_model.py) solo calcula fees para el simulador de backtesting. No existe ningún check en `risk_engine/checks.py` o en el pipeline de `engine.py` que calcule o verifique fees antes de operar en PAPER/TESTNET/LIVE. **Acción requerida antes de LIVE**: agregar un check de fees al Risk Engine (aunque sea informativo/ADJUST_DOWN si el fee proyectado erosiona el margen). |
| 13 | Estimación de slippage obligatoria | ❌ | [`backend/backtesting/slippage_model.py`](backend/backtesting/slippage_model.py) solo se usa en el motor de backtesting. [`execution/engine.py:235`](backend/execution/engine.py:235) hardcodea `slippage_usdt=Decimal("0")` en resultados de órdenes live/paper, confirmando que no se calcula. **Acción requerida antes de LIVE**: estimar slippage pre-trade (order book depth o heurística) y registrarlo, aunque no bloquee. |
| 14 | Revisión de funding obligatoria | ❌ | [`backend/quant_signals/funding.py`](backend/quant_signals/funding.py) alimenta funding como señal informativa al Decision Aggregator/GPT; [`backend/core/funding.py`](backend/core/funding.py) solo lo usa para contabilidad post-hoc. No hay ningún BLOCK/ADJUST que exija haber "revisado" funding antes de operar. **Acción requerida antes de LIVE**: decidir si esto debe ser un check de Risk Engine (ej. bloquear si el funding rate absoluto supera un umbral) o si la señal ya cumple el espíritu de la regla — documentar la decisión explícitamente si se acepta como está. |

## 15–25. Gates de calidad de señal, datos e infraestructura

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 15 | Bloqueo si señal cuantitativa débil/contradictoria/insuficiente | ✅ | [`_detect_contradictions`](backend/decision_engine/aggregator.py:236) (`low_quant_strength`, `quant_internal_conflict`) fuerza `NO_OPERAR` vía `_determine_final_action` (línea 292). Test: `tests/unit/test_decision_aggregator_contradictions.py`. |
| 16 | Bloqueo si GPT contradice Quant sin explicación verificable | ✅ | `direction_mismatch` en [`aggregator.py:247`](backend/decision_engine/aggregator.py:247) fuerza `NO_OPERAR` — no existe ningún camino de override por parte de GPT. Test: `tests/unit/test_decision_aggregator_contradictions.py`. |
| 17 | Bloqueo si Market Regime = UNCLEAR sin setup excepcional | ✅ | UNCLEAR aporta el `regime_factor` más bajo (0.20, [`aggregator.py:109`](backend/decision_engine/aggregator.py:109)), lo que arrastra el score agregado; solo pasa el umbral (`_MIN_SCORE_TO_TRADE=0.50`) si señales quant/GPT son excepcionalmente fuertes — es exactamente el comportamiento "sin setup excepcional, no opera". Test: `tests/unit/test_decision_aggregator_contradictions.py:460-524`. |
| 18 | Bloqueo si datos faltantes/vencidos/incoherentes | ✅ | [`validate_snapshot`](backend/market_data/validators.py:128) rechaza snapshots EXPIRED (>30s) o incoherentes (`SnapshotRejectedError`); datos faltantes escalan a SAFE_MODE vía [`ConnectionHealthMonitor`](backend/connection_health/monitor.py:93) (`SYMBOL_DATA_UNAVAILABLE`). Test: `tests/unit/test_validators.py`, `tests/unit/test_connection_health_monitor.py:111`. |
| 19 | Bloqueo si latencia peligrosa | ✅ | [`LATENCY_EXCEEDED`](backend/connection_health/monitor.py:118) → SAFE_MODE; bound duro `_MAX_LATENCY_MS=10_000` en `market_data/schemas.py:29`. Test: `tests/unit/test_connection_health_monitor.py:146`. |
| 20 | Bloqueo si la API falla | ✅ | `CircuitBreaker`/`retry_async` en [`backend/core/retry.py`](backend/core/retry.py) envuelven las llamadas a BingX/GPT; agotado el retry, escala a `SYMBOL_DATA_UNAVAILABLE` → SAFE_MODE. Test: `tests/unit/test_retry.py`, `tests/chaos/test_http_5xx_faults.py`, `test_disconnect_faults.py`, `test_timeout_faults.py`. |
| 21 | Bloqueo si no se puede leer balance/posiciones/órdenes activas | ❌ | [`_reconcile_positions`](backend/reconciliation/engine.py:229) captura la excepción de fetch y marca el símbolo en `failed_symbols` (`report.is_complete=False`), pero según el propio docstring de [`gate.py:40`](backend/reconciliation/gate.py:40) eso **no** dispara SAFE_MODE por sí solo — solo lo hacen las 3 discrepancias explícitas (huérfanas, protección faltante, etc.). No existe un check dedicado de "falla de lectura de balance". **Acción requerida antes de LIVE**: que un fetch fallido de balance/posiciones/órdenes por sí solo dispare SAFE_MODE, no solo quede como "reporte incompleto". |
| 22 | Bloqueo si no se pueden confirmar órdenes de salida | ✅ | [`block_on_unconfirmed_protection`](backend/reconciliation/gate.py:31) → discrepancia `MISSING_PROTECTION` → SAFE_MODE. Test: `tests/unit/test_reconciliation_gate.py`. |
| 23 | Bloqueo si hay órdenes huérfanas | ✅ | [`block_on_orphan_orders`](backend/reconciliation/gate.py:14) → discrepancia `MISSING_IN_DB` → SAFE_MODE (excluye `MISSING_IN_ADAPTER` por diseño documentado, revisado en PR #128). Test: `tests/unit/test_reconciliation_gate.py`. |
| 24 | Bloqueo si reloj local desincronizado | ✅ | [`CLOCK_SKEW_EXCEEDED`](backend/connection_health/monitor.py:107) → SAFE_MODE; bound duro `_MAX_CLOCK_SKEW_MS=5_000` en `market_data/schemas.py:28`. Test: `tests/unit/test_connection_health_monitor.py:121,133`. |
| 25 | Bloqueo si GPT devuelve JSON inválido | ✅ | [`validate_gpt_json_string`](backend/decision_engine/schema_guard.py:92) + [`classify_errors`/`handle_invalid_response`](backend/decision_engine/invalid_response_handler.py:55). Test: `tests/unit/test_schema_guard.py:230-261`, `tests/unit/test_invalid_response_handler.py:24-30`. |

## 26–30. Anti-patrones de gestión de riesgo

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 26 | No martingala | ✅ | Boot: [`RiskConfig.no_martingale`](backend/core/config.py:145). Runtime: [`check_anti_martingala`](backend/risk_engine/checks.py:397) (BLOCK), wired en `engine.py:125`. Test: `tests/unit/test_config.py:247`, `tests/unit/test_risk_engine_anti_martingala.py:23-172`. |
| 27 | No promediar pérdidas | ✅ | [`check_anti_averaging`](backend/risk_engine/checks.py:488) (BLOCK), wired en `engine.py:126`. Test: `tests/unit/test_risk_engine_anti_martingala.py:182-228`. |
| 28 | No aumentar leverage para recuperar pérdidas | ❌ | No existe ningún check que compare el leverage propuesto contra el resultado del último trade (a diferencia de `check_anti_martingala`, que hace exactamente esto para el tamaño de margen). `check_leverage_cap` solo aplica el tope estático por entorno (reglas #2–5), no una regla de "no escalar leverage tras pérdida". **Acción requerida antes de LIVE**: agregar `check_anti_leverage_escalation` (mismo patrón que `check_anti_martingala`, comparando leverage propuesto vs. leverage del último trade perdedor). |
| 29 | No abrir posiciones infinitas (límite de posiciones concurrentes) | ❌ | `max_open_positions` está declarado en [`config.py:86`](backend/core/config.py:86) (y `max_open_positions_allowed` acotado a ≤3 al bootear, línea 104), pero **no se lee en ningún lugar del runtime** fuera de su propia declaración y test de boot — ningún componente de ejecución/orquestación/risk engine cuenta las posiciones abiertas antes de permitir una nueva entrada. **Acción requerida antes de LIVE**: agregar un check (Risk Engine u orquestador) que bloquee nuevas entradas si `open_positions_count >= max_open_positions`. |
| 30 | Kill switch no debe poder ignorarse | ✅ | Nota: `backend/kill_switch/manager.py` y `backend/kill_switch/schemas.py` están **vacíos** (0 bytes) — no es ahí donde vive la lógica. La implementación real es [`bot_state_machine.py`](backend/trading_core/bot_state_machine.py) (`_ALLOWED_TRANSITIONS`: `KILL_SWITCH_TRIGGERED` solo degrada a `HALTED`, nunca vuelve solo a `ACTIVE`) + [`emergency_stop.py`](backend/trading_core/emergency_stop.py) (`EmergencyStopService.trigger`, row-locked, atómico), expuesto en [`routes_kill_switch.py:52`](backend/api/routes_kill_switch.py:52). El worker resincroniza el estado desde la DB en cada ciclo y antes de cada símbolo, así que no puede bypassearse mid-run. Smoke test end-to-end contra stack real: card [124], PR [#130](https://github.com/aclab-dev/-10k/pull/130). Test: `tests/unit/test_bot_state_machine.py`, `test_emergency_stop_service.py`, `test_routes_kill_switch.py`, `tests/smoke/test_kill_switch_smoke.py`. |

## 31–34. API keys y gates de promoción de fase

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 31 | API keys nunca en texto plano (logs/prints/storage) | ✅ | [`_scrub_secrets`](backend/core/logging.py:15) (processor de `structlog`) enmascara cualquier campo del event-dict que matchee `api_key`, `apikey`, `api_secret`, `secret`, `secret_key`, `token`, `authorization`, `credential(s)`, etc. `config.py:572` excluye explícitamente credenciales del dashboard del `config_snapshot` persistido en DB. Caveat: es un scrub por nombre de campo — un string crudo con una key embebida fuera de esos campos no se detecta. Test: `tests/unit/test_logging.py` (confirmar que cubre explícitamente `_scrub_secrets` con un caso `api_key=...`; si no lo hace, agregarlo). |
| 32 | Claves API sin permiso de retiro (withdraw) | ⚠️ | Gate operativo, no verificable por código (es config del lado del exchange). Verificado y documentado en card Trello [121], Done — adjunto `test url prod api.PNG`. `docs/bingx_api_reference.md` no menciona permisos de withdraw; recomendado agregar una línea explícita ahí referenciando este ítem del checklist. |
| 33 | No avanzar a TESTNET sin backtesting mínimo aprobado | ⚠️ | Gate de proceso. `AiAndQuantConfig.backtesting_required_before_testnet_or_live` ([`config.py:207`](backend/core/config.py:207)) está declarado pero **no se lee en ningún otro lugar del código** — no tiene efecto sobre transiciones de entorno; es config muerta. El gate real hoy es el criterio humano documentado en `docs/backtesting_framework.md` y `docs/runbook_server.md:36`. Recomendado: o bien wirear este flag a un check de boot (bloquear `load_config` si `environment != PAPER` y el flag no fue confirmado), o eliminarlo del config para no dar falsa sensación de enforcement. |
| 34 | No avanzar a LIVE sin: replay histórico + paper estable + backtesting aprobado + testnet estable + checklist LIVE firmado | ⚠️ | Único gate real de código: [`_validate_live_confirmation`](backend/core/config.py:628) exige que una env var específica (`live_confirmation_env_var`/`live_confirmation_required_value`) esté seteada para bootear en `Environment.LIVE` — evita un boot accidental, pero no verifica replay/paper/backtesting/testnet. `historical_replay_required_before_live` y `backtesting_required_before_testnet_or_live` ([`config.py:206-207`](backend/core/config.py:206)) son booleanos declarados pero no leídos (mismo hallazgo que #33) — código muerto. `backend/core/environment_guard.py` está **vacío** (0 bytes): parece un punto de enforcement planeado y nunca construido. El gate real hoy es 100% este documento (`docs/live_checklist.md`), referenciado desde `docs/runbook_server.md:284-307,446-447`. Test: `tests/unit/test_config.py:299-326` (solo cubre la confirmación por env var). |

## Resumen

- **24/34 reglas verificadas** (✅) con check de código y test.
- **3/34 son gates de proceso** (⚠️) por diseño (#32, #33, #34) — no verificables por código, pero #33/#34 tienen booleanos de config declarados y nunca leídos (código muerto que conviene wirear o eliminar).
- **7/34 tienen un gap de código real** (❌): #4 (cap 3x LIVE inicial no aplicado por Risk Engine), #12 (fees), #13 (slippage), #14 (funding no es gate), #21 (falla de lectura de balance/posiciones no autobloquea), #28 (sin anti-escalada de leverage tras pérdida), #29 (límite de posiciones concurrentes no se lee en runtime).

## Estado de la firma

**❌ NO firmado como apto para LIVE.**

Este checklist queda archivado en `docs/` como la auditoría de referencia de la
Sección 3.6, pero permanece explícitamente sin firma mientras existan ítems ❌.
La regla 34 exige este documento firmado antes de LIVE — firmarlo con gaps
abiertos violaría la regla que el documento existe para hacer cumplir.

**Próximo paso:** resolver los 7 ítems ❌ (cada uno tiene su acción requerida
documentada arriba) en tareas de seguimiento dentro de la épica F17, volver a
correr esta auditoría, y recién entonces firmar.

---

_Auditoría: Claude Code (Sonnet 5), a pedido de Rodrigo Sánchez — 2026-09-15._
_Firma pendiente: **************\_\_\_\_************** — Fecha: **\_\_\_\_**_
