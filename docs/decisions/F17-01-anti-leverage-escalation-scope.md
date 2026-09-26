# F17-01 — Alcance de "último trade" en anti-escalada de leverage

**Estado**: Aceptada  
**Fecha**: 2026-09-24  
**Epic**: F17 — Preparación LIVE  
**Card**: https://trello.com/c/ezM70Itk (regla 28 de `docs/live_checklist.md`)

---

## Contexto

La regla no negociable de la Sección 3.6 del PDF maestro dice: *"No aumentar
apalancamiento para recuperar perdidas"*. `check_anti_leverage_escalation`
la implementa comparando el leverage propuesto contra el del último trade
cerrado cuando ese trade perdió.

La card pide definir explícitamente qué es "el último trade": por símbolo o
global. El spec no lo dice textualmente, y el precedente en el código
(`check_anti_martingala`) usa el último trade **del símbolo**
(`TradeRepository.get_last_closed_trade(bot_run_id, symbol)`).

---

## Decisión

"El último trade" es el último trade cerrado del bot run **en cualquier
símbolo** (global a la cuenta), vía
`TradeRepository.get_last_closed_trade_any_symbol(bot_run_id)`.

El Risk Engine lo recibe por kwargs propios
(`last_account_trade_pnl_usdt`, `last_account_trade_leverage`), separados de
los `last_trade_*` por símbolo que sigue usando anti-martingala.

Lo que se compara contra ese trade es el leverage **propuesto** por la
decisión (`original_leverage`), no el que resultaría de `check_leverage_cap`.
Si el modelo propone 20x tras perder a 10x, se bloquea aunque el cap lo
hubiera bajado a 10x.

---

## Fundamento en el spec

1. **Las pérdidas del sistema son de la cuenta, no del par.** La tabla de
   límites (§1) fija la pérdida diaria (10%) y total (50%) sobre el *capital
   total*, y la Sección 3.6 las repite como reglas no negociables. La
   pérdida que se intentaría "recuperar" es de ese capital: perder en
   BTCUSDT y subir el leverage en SOLUSDT es exactamente el patrón que la
   regla prohíbe. Con alcance por símbolo, rotar de par
   bastaría para esquivarla.
2. **§2 Principios rectores — "Seguridad primero"**: *ninguna rentabilidad
   potencial justifica violar límites de riesgo*. El costo del alcance global
   son falsos positivos (un trade legítimo en otro par con más leverage queda
   bloqueado → no se opera). El del alcance por símbolo es dejar pasar el
   comportamiento prohibido. El spec prioriza lo primero.

3. **§3.9 Risk Engine corregido**: *"No interpreta deseos del modelo: valida
   condiciones duras"*. Todas las condiciones del pseudocódigo de `validate`
   se evalúan sobre los campos de la decisión tal como llegan
   (`require(decision.leverage <= max_leverage_for_environment(...))`, no
   sobre un valor ya ajustado), y el ajuste queda al final
   (`APPROVE_OR_ADJUST_TO_EXCHANGE_FILTERS`). Una propuesta que sube el
   leverage tras una pérdida es el patrón prohibido aunque el cap la recorte;
   el cap no la vuelve válida. Es el mismo criterio que `check_anti_martingala`
   aplica con `original_margin`.

## Alternativa descartada

**Por símbolo**, por consistencia con anti-martingala y porque el leverage
razonable varía por par (Decision 07, leverage dinámico según volatilidad).
Se descarta porque el leverage dinámico es un argumento para que el Risk
Engine *reduzca* leverage, no para permitir subirlo tras una pérdida; y
porque deja abierto el bypass por rotación de símbolo descrito arriba.

---

## Consecuencias

- Tras un trade perdedor, el leverage de ese trade pasa a ser un **techo**
  para todos los símbolos. No es un bloqueo total: se siguen abriendo trades
  con leverage igual o menor. Sólo se bloquean las propuestas que lo superan.
- El techo se levanta únicamente cuando cierra un trade no perdedor, y ese
  trade tuvo que abrirse respetando el techo. Si en cambio cierra otro trade
  perdedor, su leverage (≤ al techo) pasa a ser el nuevo techo: tras
  pérdidas consecutivas el techo sólo puede bajar.
- Mientras el modelo sólo proponga leverage por encima del techo, no se abre
  nada: todas esas propuestas quedan en BLOCK (auditadas en
  `risk_validations`) hasta que proponga uno igual o menor.
- `check_anti_martingala` sigue siendo por símbolo; esta decisión no lo
  modifica (fuera del alcance de la card).
- El Historical Replay no provee historial de trades al Risk Engine (igual que
  para anti-martingala), así que el check no se evalúa ahí.
