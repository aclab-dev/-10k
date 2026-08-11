import type { ReactNode } from 'react'
import { PAGE_SIZE } from '../hooks/useHistory'

export interface Column<T> {
  key: string
  header: string
  render: (row: T) => ReactNode
}

interface HistoryTableProps<T> {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string
  onSelect: (row: T) => void
  selectedKey: string | null
  total: number
  offset: number
  onOffsetChange: (offset: number) => void
  loading: boolean
  emptyMessage: string
}

/** Rango humano de la página actual: "1–25 de 132". */
function rangeLabel(offset: number, shown: number, total: number): string {
  if (total === 0) return '0 de 0'
  return `${offset + 1}–${offset + shown} de ${total}`
}

export function HistoryTable<T>({
  columns,
  rows,
  rowKey,
  onSelect,
  selectedKey,
  total,
  offset,
  onOffsetChange,
  loading,
  emptyMessage,
}: HistoryTableProps<T>) {
  const hasPrev = offset > 0
  const hasNext = offset + rows.length < total

  return (
    <div className="history-table-wrapper">
      <table className="history-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key}>{column.header}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = rowKey(row)
            return (
              <tr
                key={key}
                className={key === selectedKey ? 'row-selected' : undefined}
                aria-selected={key === selectedKey}
              >
                {columns.map((column, index) => (
                  <td key={column.key}>
                    {index === 0 ? (
                      // El botón va en la primera celda para que la fila sea
                      // accesible por teclado: <tr onClick> no es focuseable.
                      <button type="button" className="row-button" onClick={() => onSelect(row)}>
                        {column.render(row)}
                      </button>
                    ) : (
                      column.render(row)
                    )}
                  </td>
                ))}
              </tr>
            )
          })}
        </tbody>
      </table>

      {rows.length === 0 && !loading && <p className="empty-hint">{emptyMessage}</p>}
      {loading && <p className="empty-hint">Cargando…</p>}

      <div className="pagination">
        <button
          type="button"
          disabled={!hasPrev || loading}
          onClick={() => onOffsetChange(Math.max(0, offset - PAGE_SIZE))}
        >
          Anterior
        </button>
        <span className="pagination-range">{rangeLabel(offset, rows.length, total)}</span>
        <button
          type="button"
          disabled={!hasNext || loading}
          onClick={() => onOffsetChange(offset + PAGE_SIZE)}
        >
          Siguiente
        </button>
      </div>
    </div>
  )
}
