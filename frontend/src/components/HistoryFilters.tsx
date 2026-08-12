import type { HistoryFiltersState } from '../hooks/useHistory'

/** Los cinco pares habilitados por las reglas del proyecto. */
const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT'] as const

interface HistoryFiltersProps {
  filters: HistoryFiltersState
  onChange: (filters: HistoryFiltersState) => void
  /** Etiqueta del filtro que cambia por pestaña: "Acción" o "Resultado". */
  kindLabel: string
  kindOptions: readonly string[]
}

export function HistoryFilters({
  filters,
  onChange,
  kindLabel,
  kindOptions,
}: HistoryFiltersProps) {
  function update(patch: Partial<HistoryFiltersState>) {
    onChange({ ...filters, ...patch })
  }

  const rangeInvalid = filters.from !== '' && filters.to !== '' && filters.from > filters.to

  return (
    <div className="history-filters">
      <label>
        Desde
        <input
          type="datetime-local"
          value={filters.from}
          onChange={(e) => update({ from: e.target.value })}
        />
      </label>
      <label>
        Hasta
        <input
          type="datetime-local"
          value={filters.to}
          onChange={(e) => update({ to: e.target.value })}
        />
      </label>
      <label>
        Símbolo
        <select value={filters.symbol} onChange={(e) => update({ symbol: e.target.value })}>
          <option value="">Todos</option>
          {SYMBOLS.map((symbol) => (
            <option key={symbol} value={symbol}>
              {symbol}
            </option>
          ))}
        </select>
      </label>
      <label>
        {kindLabel}
        <select value={filters.kind} onChange={(e) => update({ kind: e.target.value })}>
          <option value="">Todos</option>
          {kindOptions.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </label>
      {rangeInvalid && (
        <p className="error-message" role="alert">
          El inicio del rango es posterior al fin.
        </p>
      )}
      <p className="filters-hint">Las fechas se interpretan en la hora local del navegador.</p>
    </div>
  )
}
