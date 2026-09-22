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
| 1 | Margen máx. 10 USDT/operación | ✅ | Boot: [`ChallengeConfig.margin_le_10`](../backend/core/config.py#L68), [`RiskConfig.margin_le_10`](../backend/core/config.py#L138). Runtime: [`check_margin_cap`](../backend/risk_engine/checks.py#L176) (ADJUST_DOWN), wired en [`engine.py:165`](../backend/risk_engine/engine.py#L165). Test: `tests/unit/test_config.py:222,227`, `tests/unit/test_risk_engine_exhaustive.py`. |
| 2 | Leverage máx. 10x PAPER | ✅ | Boot: [`LeverageConfig`](../backend/core/config.py#L163) (`max_leverage_paper>10` rechaza, línea 174). Runtime: [`leverage_cap_for_env`](../backend/risk_engine/checks.py#L48) + [`check_leverage_cap`](../backend/risk_engine/checks.py#L194). Test: `tests/unit/test_config.py:232`. |
| 3 | Leverage máx. 5x TESTNET | ✅ | Boot: `config.py:178`. Runtime: igual que #2. Test: `tests/unit/test_config.py:237`. |
| 4 | Leverage máx. 3x LIVE inicial | ❌ | Boot valida `max_leverage_live_initial<=3` ([`config.py:186`](../backend/core/config.py#L186)), pero el Risk Engine **no lo usa**: [`leverage_cap_for_env`](../backend/risk_engine/checks.py#L48) devuelve `max_leverage_live_absolute` (5x) para cualquier entorno LIVE — el cap de 3x solo se aplica en la capa de sugerencia ([`volatility/leverage.py:63`](../backend/volatility/leverage.py#L63), comentario propio admite que falta el mecanismo de promoción de fase). Una decisión de 4x–5x en LIVE inicial no sería bloqueada por el Risk Engine. **Acción requerida antes de LIVE**: hacer que `leverage_cap_for_env` distinga LIVE-inicial de LIVE-absoluto (vía un flag de fase, no solo el entorno), o bloquear explícitamente >3x mientras no exista ese mecanismo. |
| 5 | Leverage máx. 5x absoluto LIVE | ✅ | Boot: `config.py:182`. Runtime: igual que #2 (cap efectivo hoy, ver nota en #4). Test: `tests/unit/test_config.py:242`. |
| 6 | Pérdida diaria máx. 10% | ✅ | [`check_daily_drawdown`](../backend/risk_engine/checks.py#L111) (BLOCK), wired en `engine.py:128`. Test: `tests/unit/test_risk_engine_exhaustive.py`. |
| 7 | Pérdida total máx. 50% | ✅ | [`check_total_drawdown`](../backend/risk_engine/checks.py#L142) (BLOCK), wired en `engine.py:129`. Test: `tests/unit/test_risk_engine_exhaustive.py`. |
| 8 | Símbolos permitidos: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT | ✅ | [`TradingConfig.symbols_in_whitelist`](../backend/core/config.py#L91) (boot) + bound duplicado en `backend/market_data/schemas.py` (`ALLOWED_SYMBOLS`). Test: `tests/unit/test_config.py:50,262`, `tests/unit/test_market_snapshot.py`. |
| 9 | Prohibido cross margin (ISOLATED obligatorio) | ✅ | [`TradingConfig.no_cross_margin`](../backend/core/config.py#L99) y [`RiskConfig.no_cross`](../backend/core/config.py#L152) — ambos rechazan `CROSS` al bootear. Test: `tests/unit/test_config.py:252,278`. Verificación operativa en cuenta real: card Trello [122], Done. |

## 10–14. Obligaciones pre-trade

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 10 | Stop loss obligatorio | ✅ | [`check_sl_required`](../backend/risk_engine/checks.py#L64) (BLOCK) + defensa redundante dentro de [`check_liquidation_safety`](../backend/risk_engine/checks.py#L274). Test: `tests/unit/test_risk_engine_exhaustive.py`. |
| 11 | Take profit o plan de salida obligatorio | ✅ | [`check_tp_or_exit_plan`](../backend/risk_engine/checks.py#L79) (BLOCK). Test: `tests/unit/test_risk_engine_exhaustive.py`. |
| 12 | Cálculo de fees obligatorio | ❌ | [`backend/backtesting/fee_model.py`](../backend/backtesting/fee_model.py) solo calcula fees para el simulador de backtesting. No existe ningún check en `risk_engine/checks.py` o en el pipeline de `engine.py` que calcule o verifique fees antes de operar en PAPER/TESTNET/LIVE. **Acción requerida antes de LIVE**: agregar un check de fees al Risk Engine (aunque sea informativo/ADJUST_DOWN si el fee proyectado erosiona el margen). |
| 13 | Estimación de slippage obligatoria | ✅ | Estimación pre-trade: [`estimate_slippage`](../backend/core/slippage.py#L129) + [`estimate_for_decision`](../backend/core/slippage.py#L210) — heurística documentada (media horquilla bid/ask + impacto fijo en BPS, `slippage.market_impact_bps` en `config.yaml`), no order book depth: el codebase no tiene profundidad de libro, `MarketSnapshot` sólo expone bid/ask/spread. Llega al Risk Engine vía [`check_slippage_estimate`](../backend/risk_engine/checks.py#L612), wired en [`engine.py:146`](../backend/risk_engine/engine.py#L146), y queda en `reasons` en los tres caminos de salida — también en los trades que el Risk Engine rechaza. **Dónde queda persistido**: en `orders.estimated_slippage_usdt` y en `risk_validations.reasons` (Anexo B). Esa tabla no la escribía nadie —`to_db_kwargs()` no tenía un solo caller y `risk_validations` sólo se leía desde `routes_risk.py` y `replay/comparison.py`—, así que esta card cablea la cadena entera en [`CycleRunner._process_symbol`](../backend/trading_core/cycle_runner.py#L406): `decisions` → `decision_aggregations` → `risk_validations`, en ese orden por sus FKs, detrás de los flags `storage.log_all_decisions` y `storage.log_risk_validations` que ya existían en `config.yaml`. La agregación se persiste antes del gate de NO_OPERAR (una decisión sin edge es tan auditable como una ejecutada) y la validación en sus cuatro resultados. **Es informativo por diseño**: la regla pide estimar y registrar, no vetar por magnitud; qué slippage es "demasiado" depende del edge del trade, que el check no conoce. Para que eso no sea fail-open silencioso, los dos call sites reales lo calculan siempre ([`cycle_runner.py:415`](../backend/trading_core/cycle_runner.py#L415), [`historical_replay_engine.py`](../backend/replay/historical_replay_engine.py)) y la ausencia del dato se asienta explícita en `reasons`. Slippage real post-fill: el `Decimal("0")` hardcodeado ya no existe — cuando el adapter mide, el valor se persiste en `orders.slippage_usdt` ([`execution/engine.py:362`](../backend/execution/engine.py#L362), migración `e5b3a71c9d40`) y el replay idempotente lo lee de ahí ([`engine.py:251`](../backend/execution/engine.py#L251)). El estimado se guarda en la misma fila (`orders.estimated_slippage_usdt`), así comparar estimado vs. real es una lectura de una sola fila. **Alcance de la medición real**: sólo PAPER la tiene hoy. `OrderResult.slippage_usdt` es `Decimal | None` y [`BingXAdapter` no lo mide](../backend/exchange_adapters/bingx_adapter.py#L576) devuelve `None` — BingX no reporta slippage y no es derivable de su respuesta (`_parse_order` también corre desde `_query_order`/`get_order_status`, sin precio de referencia, y el POST de una MARKET suele volver con `avgPrice=0`). `None` significa "no se midió" y **no** se persiste como 0, así que en TESTNET/LIVE la columna queda NULL en vez de sesgar la comparación con un cero falso. La regla 13 igual se cumple en todos los entornos porque lo que exige es la *estimación*, que no depende del adapter; medir el real en LIVE requiere que el adapter lo reporte y queda como mejora, no como gap de esta regla. **Coherencia estimador/simulador**: el fill simulado de PAPER cruza el mismo bid/ask contra el que se estimó — `SlippageEstimate` transporta el libro hasta la `OrderRequest`, y [`PaperAdapter._cross_spread`](../backend/exchange_adapters/paper_adapter.py#L349) aplica la media horquilla además del impacto en BPS. Antes el simulador llenaba al precio de referencia sin cruzar el spread, así que el real contenía sólo el impacto y el estimado lo superaba de forma estructural por exactamente el medio spread. Consecuencia a tener presente al leer los datos: en PAPER estimado y real coinciden por construcción (ambos salen del mismo modelo y del mismo libro), de modo que la comparación ahí verifica el plumbing, no la calidad de la heurística; **el error real del modelo sólo se mide contra fills de exchange, en TESTNET/LIVE**. **El cruce aplica a entradas y salidas**: [`PositionManager._place_close_order`](../backend/position_manager/manager.py#L600) recibe el `(bid, ask)` del símbolo y lo pasa a su `OrderRequest`, así que los cierres simulados (SL, TP, trailing, invalidación) también pagan la media horquilla. El libro viaja por el mismo camino que el `mark_price`: [`MarketDataCycleService.get_last_book`](../backend/market_data/cycle_service.py#L129) → `PositionTickService` → `PositionManager.tick(book=...)`. Es opcional en toda la cadena: si todavía no hay snapshot del símbolo el cierre llena al `mark_price`, porque no cerrar una posición sería peor que cerrarla sin cruzar el spread. Sin esto las entradas pagaban la horquilla y las salidas no, y el PnL de PAPER quedaba optimista por medio spread en cada cierre — sesgo sobre los datos con los que se decide promover a TESTNET. Nota: `ModelDecision.estimated_slippage_usdt` es una autoestimación de GPT y no cuenta como cumplimiento — GPT no es el edge. Test: `tests/unit/test_slippage_estimator.py`, `tests/unit/test_risk_engine_slippage.py`, `tests/unit/test_execution_engine.py` (persistencia real + estimado, y replay idempotente devolviendo el valor persistido en vez de 0). Card Trello [162]. |
| 14 | Revisión de funding obligatoria | ✅ | Gate en el Risk Engine (F17): [`check_funding_gate`](../backend/risk_engine/checks.py#L217), cableado en la fase BLOCK de [`validate`](../backend/risk_engine/engine.py#L49) con el `funding_rate` del snapshot del ciclo (`cycle_runner` y `historical_replay_engine`). Bloquea si el funding que el trade **paga** alcanza el umbral (LONG con rate > 0, SHORT con rate < 0; el funding a favor nunca bloquea; `>=` bloquea). Umbral y política de dato faltante en `config.yaml` → `funding_gate` (`enabled`, `max_adverse_funding_rate`=0.001, `block_if_funding_unknown`=true), validado al boot por [`FundingGateConfig`](../backend/core/config.py#L380) (rango (0, 1)); overrides `BOT__FUNDING_GATE__*` en `.env.example`. Funding desconocido (`None`) bloquea (fail-closed) mientras `block_if_funding_unknown=true`. La señal informativa del Aggregator/GPT se mantiene, pero ya no es lo que satisface la regla. Tests: `TestFundingGateCheck`, `TestRiskEngineFundingGateIntegration` y `TestFundingGateConfigValidation` en `tests/unit/test_risk_engine_validation.py`. |

## 15–25. Gates de calidad de señal, datos e infraestructura

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 15 | Bloqueo si señal cuantitativa débil/contradictoria/insuficiente | ✅ | [`_detect_contradictions`](../backend/decision_engine/aggregator.py#L236) (`low_quant_strength`, `quant_internal_conflict`) fuerza `NO_OPERAR` vía [`_determine_final_action`](../backend/decision_engine/aggregator.py#L293). Test: `tests/unit/test_decision_aggregator_contradictions.py`. |
| 16 | Bloqueo si GPT contradice Quant sin explicación verificable | ✅ | `direction_mismatch` en [`aggregator.py:247`](../backend/decision_engine/aggregator.py#L247) fuerza `NO_OPERAR` — no existe ningún camino de override por parte de GPT. Test: `tests/unit/test_decision_aggregator_contradictions.py`. |
| 17 | Bloqueo si Market Regime = UNCLEAR sin setup excepcional | ✅ | UNCLEAR aporta el `regime_factor` más bajo (0.20, [`aggregator.py:109`](../backend/decision_engine/aggregator.py#L109)), lo que arrastra el score agregado; solo pasa el umbral (`_MIN_SCORE_TO_TRADE=0.50`) si señales quant/GPT son excepcionalmente fuertes — es exactamente el comportamiento "sin setup excepcional, no opera". Test: `tests/unit/test_decision_aggregator_contradictions.py:460-524`. |
| 18 | Bloqueo si datos faltantes/vencidos/incoherentes | ✅ | [`validate_snapshot`](../backend/market_data/validators.py#L128) rechaza snapshots EXPIRED (>30s) o incoherentes (`SnapshotRejectedError`); datos faltantes escalan a SAFE_MODE vía [`ConnectionHealthMonitor`](../backend/connection_health/monitor.py#L58) (`SYMBOL_DATA_UNAVAILABLE`). Test: `tests/unit/test_validators.py`, `tests/unit/test_connection_health_monitor.py:111`. |
| 19 | Bloqueo si latencia peligrosa | ✅ | [`LATENCY_EXCEEDED`](../backend/connection_health/monitor.py#L122) → SAFE_MODE; bound duro `_MAX_LATENCY_MS=10_000` en `market_data/schemas.py:29`. Test: `tests/unit/test_connection_health_monitor.py:146`. |
| 20 | Bloqueo si la API falla | ✅ | `CircuitBreaker`/`retry_async` en [`backend/core/retry.py`](../backend/core/retry.py) envuelven las llamadas a BingX/GPT; agotado el retry, escala a `SYMBOL_DATA_UNAVAILABLE` → SAFE_MODE. Test: `tests/unit/test_retry.py`, `tests/chaos/test_http_5xx_faults.py`, `test_disconnect_faults.py`, `test_timeout_faults.py`. |
| 21 | Bloqueo si no se puede leer balance/posiciones/órdenes activas | ❌ | [`_reconcile_positions`](../backend/reconciliation/engine.py#L219) captura la excepción de fetch y marca el símbolo en `failed_symbols` (`report.is_complete=False`), pero según el propio docstring de [`gate.py:40`](../backend/reconciliation/gate.py#L40) eso **no** dispara SAFE_MODE por sí solo — solo lo hacen las 3 discrepancias explícitas (huérfanas, protección faltante, etc.). No existe un check dedicado de "falla de lectura de balance". **Acción requerida antes de LIVE**: que un fetch fallido de balance/posiciones/órdenes por sí solo dispare SAFE_MODE, no solo quede como "reporte incompleto". |
| 22 | Bloqueo si no se pueden confirmar órdenes de salida | ✅ | `block_on_unconfirmed_protection` (flag de `config.yaml`, contexto en el [docstring del módulo](../backend/reconciliation/gate.py#L31)) evaluado en [`ReconciliationGate._blocking_reasons`](../backend/reconciliation/gate.py#L177) → discrepancia `MISSING_PROTECTION` → SAFE_MODE. Test: `tests/unit/test_reconciliation_gate.py`. |
| 23 | Bloqueo si hay órdenes huérfanas | ✅ | `block_on_orphan_orders` (flag de `config.yaml`, contexto en el [docstring del módulo](../backend/reconciliation/gate.py#L14)) evaluado en [`ReconciliationGate._blocking_reasons`](../backend/reconciliation/gate.py#L161) contra [`_ORPHAN_ORDER_TYPES`](../backend/reconciliation/gate.py#L89) → discrepancia `MISSING_IN_DB` → SAFE_MODE (excluye `MISSING_IN_ADAPTER` por diseño documentado, revisado en PR #128). Test: `tests/unit/test_reconciliation_gate.py`. |
| 24 | Bloqueo si reloj local desincronizado | ✅ | [`CLOCK_SKEW_EXCEEDED`](../backend/connection_health/monitor.py#L111) → SAFE_MODE; bound duro `_MAX_CLOCK_SKEW_MS=5_000` en `market_data/schemas.py:28`. Test: `tests/unit/test_connection_health_monitor.py:121,133`. |
| 25 | Bloqueo si GPT devuelve JSON inválido | ✅ | [`validate_gpt_json_string`](../backend/decision_engine/schema_guard.py#L92) + [`classify_errors`/`handle_invalid_response`](../backend/decision_engine/invalid_response_handler.py#L55). Test: `tests/unit/test_schema_guard.py:230-261`, `tests/unit/test_invalid_response_handler.py:24-30`. |

## 26–30. Anti-patrones de gestión de riesgo

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 26 | No martingala | ✅ | Boot: [`RiskConfig.no_martingale`](../backend/core/config.py#L145). Runtime: [`check_anti_martingala`](../backend/risk_engine/checks.py#L460) (BLOCK), wired en `engine.py:131`. Test: `tests/unit/test_config.py:247`, `tests/unit/test_risk_engine_anti_martingala.py:23-172`. |
| 27 | No promediar pérdidas | ✅ | [`check_anti_averaging`](../backend/risk_engine/checks.py#L551) (BLOCK), wired en `engine.py:132`. Test: `tests/unit/test_risk_engine_anti_martingala.py:182-228`. |
| 28 | No aumentar leverage para recuperar pérdidas | ❌ | No existe ningún check que compare el leverage propuesto contra el resultado del último trade (a diferencia de `check_anti_martingala`, que hace exactamente esto para el tamaño de margen). `check_leverage_cap` solo aplica el tope estático por entorno (reglas #2–5), no una regla de "no escalar leverage tras pérdida". **Acción requerida antes de LIVE**: agregar `check_anti_leverage_escalation` (mismo patrón que `check_anti_martingala`, comparando leverage propuesto vs. leverage del último trade perdedor). |
| 29 | No abrir posiciones infinitas (límite de posiciones concurrentes) | ❌ | `max_open_positions` está declarado en [`config.py:86`](../backend/core/config.py#L86) (y `max_open_positions_allowed` acotado a ≤3 al bootear, línea 104), pero **no se lee en ningún lugar del runtime** fuera de su propia declaración y test de boot — ningún componente de ejecución/orquestación/risk engine cuenta las posiciones abiertas antes de permitir una nueva entrada. **Acción requerida antes de LIVE**: agregar un check (Risk Engine u orquestador) que bloquee nuevas entradas si `open_positions_count >= max_open_positions`. |
| 30 | Kill switch no debe poder ignorarse | ✅ | Nota: `backend/kill_switch/manager.py` y `backend/kill_switch/schemas.py` están **vacíos** (0 bytes) — no es ahí donde vive la lógica. La implementación real es [`bot_state_machine.py`](../backend/trading_core/bot_state_machine.py) (`_ALLOWED_TRANSITIONS`: `KILL_SWITCH_TRIGGERED` solo degrada a `HALTED`, nunca vuelve solo a `ACTIVE`) + [`emergency_stop.py`](../backend/trading_core/emergency_stop.py) (`EmergencyStopService.trigger`, row-locked, atómico), expuesto en [`routes_kill_switch.py:52`](../backend/api/routes_kill_switch.py#L52). El worker resincroniza el estado desde la DB en cada ciclo y antes de cada símbolo, así que no puede bypassearse mid-run. Smoke test end-to-end contra stack real: card [124], PR [#130](https://github.com/aclab-dev/-10k/pull/130). Test: `tests/unit/test_bot_state_machine.py`, `test_emergency_stop_service.py`, `test_routes_kill_switch.py`, `tests/smoke/test_kill_switch_smoke.py`. |

## 31–34. API keys y gates de promoción de fase

| # | Regla | Estado | Evidencia |
|---|---|---|---|
| 31 | API keys nunca en texto plano (logs/prints/storage) | ✅ | [`_scrub_secrets`](../backend/core/logging.py#L37) (processor de `structlog`) enmascara cualquier campo del event-dict que matchee `api_key`, `apikey`, `api_secret`, `secret`, `secret_key`, `token`, `authorization`, `credential(s)`, etc. `config.py:593` excluye explícitamente credenciales del dashboard del `config_snapshot` persistido en DB. Caveat: es un scrub por nombre de campo — un string crudo con una key embebida fuera de esos campos no se detecta (ver acción requerida en el resumen). Test: `tests/unit/test_logging.py:20` (`test_masks_api_key`), `tests/unit/test_logging.py:54` (`test_all_secret_keys_are_masked`). |
| 32 | Claves API sin permiso de retiro (withdraw) | ⚠️ | Gate operativo, no verificable por código (es config del lado del exchange). Verificado y documentado en card Trello [121], Done — adjunto `test url prod api.PNG`. `docs/bingx_api_reference.md` no menciona permisos de withdraw; recomendado agregar una línea explícita ahí referenciando este ítem del checklist. |
| 33 | No avanzar a TESTNET sin backtesting mínimo aprobado | ⚠️ | Gate de proceso. `AiAndQuantConfig.backtesting_required_before_testnet_or_live` ([`config.py:207`](../backend/core/config.py#L207)) está declarado pero **no se lee en ningún otro lugar del código** — no tiene efecto sobre transiciones de entorno; es config muerta. El gate real hoy es el criterio humano documentado en `docs/runbook_server.md:36`. `docs/backtesting_framework.md` está **vacío** (0 bytes) — el gate de proceso ni siquiera tiene el documento que debería respaldarlo, lo que refuerza el hallazgo de config muerta en vez de compensarlo. Recomendado: o bien wirear este flag a un check de boot (bloquear `load_config` si `environment != PAPER` y el flag no fue confirmado), o eliminarlo del config para no dar falsa sensación de enforcement. |
| 34 | No avanzar a LIVE sin: replay histórico + paper estable + backtesting aprobado + testnet estable + checklist LIVE firmado | ⚠️ | Único gate real de código: [`_validate_live_confirmation`](../backend/core/config.py#L681) exige que una env var específica (`live_confirmation_env_var`/`live_confirmation_required_value`) esté seteada para bootear en `Environment.LIVE` — evita un boot accidental, pero no verifica replay/paper/backtesting/testnet. `historical_replay_required_before_live` y `backtesting_required_before_testnet_or_live` ([`config.py:206-207`](../backend/core/config.py#L206)) son booleanos declarados pero no leídos (mismo hallazgo que #33) — código muerto. `backend/core/environment_guard.py` está **vacío** (0 bytes): parece un punto de enforcement planeado y nunca construido. El gate real hoy es 100% este documento (`docs/live_checklist.md`), referenciado desde `docs/runbook_server.md:284-307,446-447`. Test: `tests/unit/test_config.py:299-326` (solo cubre la confirmación por env var). |

## Resumen

- **26/34 reglas verificadas** (✅) con check de código y test.
- **3/34 son gates de proceso** (⚠️) por diseño (#32, #33, #34) — no verificables por código, pero #33/#34 tienen booleanos de config declarados y nunca leídos (código muerto que conviene wirear o eliminar).
- **5/34 tienen un gap de código real** (❌): #4 (cap 3x LIVE inicial no aplicado por Risk Engine), #12 (fees), #21 (falla de lectura de balance/posiciones no autobloquea), #28 (sin anti-escalada de leverage tras pérdida), #29 (límite de posiciones concurrentes no se lee en runtime).
- **Resueltos desde la auditoría original**: #13 (slippage, card Trello [162]) y #14 (funding, card Trello [163]).

### Acciones menores sin card de seguimiento (aceptadas como están)

Estas cuatro no tienen card en la épica F17 — se aceptan explícitamente como están,
con la justificación de por qué no bloquean la firma pese a quedar mencionadas:

- **#31** — el scrub de `_scrub_secrets` es por nombre de campo; una API key
  embebida en un string crudo (fuera de esos campos) no se detecta. Se acepta
  porque el uso real del logging estructurado en este código siempre pasa las
  credenciales como campos nombrados, nunca interpoladas en un string libre.
- **#32** — falta una línea en `docs/bingx_api_reference.md` documentando que
  las API keys no deben tener permiso de retiro. Se acepta como deuda de
  documentación pura (el control ya está verificado y en Done en Trello [121]);
  no bloquea LIVE porque no es un gap de enforcement, es un gap de referencia.
- **#33/#34** — `historical_replay_required_before_live` y
  `backtesting_required_before_testnet_or_live` (`config.py:206-207`) son
  código muerto (declarados, nunca leídos). Se aceptan sin wirear porque el
  gate real de estas dos reglas ya es este mismo documento (la regla 34) y el
  criterio humano de `docs/runbook_server.md`; eliminarlos o wirearlos es
  limpieza de config, no un gap de riesgo no cubierto.

## Estado de la firma

**❌ NO firmado como apto para LIVE.**

Este checklist queda archivado en `docs/` como la auditoría de referencia de la
Sección 3.6, pero permanece explícitamente sin firma mientras existan ítems ❌.
La regla 34 exige este documento firmado antes de LIVE — firmarlo con gaps
abiertos violaría la regla que el documento existe para hacer cumplir.

**Próximo paso:** resolver los 5 ítems ❌ restantes (cada uno tiene su acción requerida
documentada arriba) en tareas de seguimiento dentro de la épica F17, volver a
correr esta auditoría, y recién entonces firmar.

---

_Auditoría: Claude Code (Sonnet 5), a pedido de Rodrigo Sánchez — 2026-09-15._
_Actualización #13 (slippage) — 2026-09-21, card Trello [162]._
_Firma pendiente: **************\_\_\_\_************** — Fecha: **\_\_\_\_**_
