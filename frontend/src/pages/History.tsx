import { useEffect, useState } from 'react'
import {
  getDecisions,
  getRiskValidations,
  type DecisionOut,
  type RiskValidationOut,
} from '../api/client'
import { DecisionDetail } from '../components/DecisionDetail'
import { HistoryFilters } from '../components/HistoryFilters'
import { HistoryTable, type Column } from '../components/HistoryTable'
import { RiskValidationDetail } from '../components/RiskValidationDetail'
import { formatTimestamp } from '../format'
import { EMPTY_FILTERS, useHistory, type HistoryFiltersState } from '../hooks/useHistory'

type Tab = 'decisions' | 'blocks'

const DECISION_ACTIONS = ['OPEN', 'CLOSE', 'NO_OPERAR'] as const
/**
 * NO_OPERAR y BLOCK son estados distintos: NO_OPERAR lo decide el Aggregator por
 * falta de edge, BLOCK es un rechazo del Risk Engine. La pestaña de bloqueos deja
 * elegir entre los cuatro para poder auditar la diferencia, no solo los BLOCK.
 */
const RISK_RESULTS = ['BLOCK', 'ADJUST_DOWN', 'APPROVE', 'NO_OPERAR'] as const

const DECISION_COLUMNS: Column<DecisionOut>[] = [
  { key: 'timestamp', header: 'Fecha', render: (row) => formatTimestamp(row.timestamp) },
  { key: 'symbol', header: 'Símbolo', render: (row) => row.symbol },
  { key: 'action', header: 'Acción', render: (row) => row.action },
  { key: 'direction', header: 'Dirección', render: (row) => row.direction ?? '—' },
  {
    key: 'confidence',
    header: 'Confianza',
    render: (row) => (row.confidence === null ? '—' : row.confidence.toFixed(2)),
  },
  { key: 'margin', header: 'Margen', render: (row) => row.margin_usdt ?? '—' },
]

const RISK_COLUMNS: Column<RiskValidationOut>[] = [
  { key: 'timestamp', header: 'Fecha', render: (row) => formatTimestamp(row.timestamp) },
  { key: 'symbol', header: 'Símbolo', render: (row) => row.symbol },
  { key: 'result', header: 'Resultado', render: (row) => row.result },
  { key: 'original_margin', header: 'Margen orig.', render: (row) => row.original_margin ?? '—' },
  {
    key: 'adjusted_margin',
    header: 'Margen ajust.',
    render: (row) => row.adjusted_margin ?? '—',
  },
]

interface HistoryProps {
  onUnauthorized: () => void
}

interface TabProps {
  onUnauthorized: () => void
}

function DecisionsTab({ onUnauthorized }: TabProps) {
  const [filters, setFilters] = useState<HistoryFiltersState>(EMPTY_FILTERS)
  const [selected, setSelected] = useState<DecisionOut | null>(null)
  const { items, total, offset, loading, error, setOffset } = useHistory(
    getDecisions,
    filters,
    'action',
  )

  useEffect(() => {
    if (error?.status === 401) onUnauthorized()
  }, [error, onUnauthorized])

  return (
    <>
      <HistoryFilters
        filters={filters}
        onChange={setFilters}
        kindLabel="Acción"
        kindOptions={DECISION_ACTIONS}
      />
      {error && <p className="error-message">{error.message}</p>}
      <HistoryTable
        columns={DECISION_COLUMNS}
        rows={items}
        rowKey={(row) => row.id}
        onSelect={setSelected}
        selectedKey={selected?.id ?? null}
        total={total}
        offset={offset}
        onOffsetChange={setOffset}
        loading={loading}
        emptyMessage="No hay decisiones para estos filtros."
      />
      {selected && (
        <DecisionDetail decisionId={selected.id} onClose={() => setSelected(null)} />
      )}
    </>
  )
}

function BlocksTab({ onUnauthorized }: TabProps) {
  const [filters, setFilters] = useState<HistoryFiltersState>({ ...EMPTY_FILTERS, kind: 'BLOCK' })
  const [selected, setSelected] = useState<RiskValidationOut | null>(null)
  const { items, total, offset, loading, error, setOffset } = useHistory(
    getRiskValidations,
    filters,
    'result',
  )

  useEffect(() => {
    if (error?.status === 401) onUnauthorized()
  }, [error, onUnauthorized])

  return (
    <>
      <HistoryFilters
        filters={filters}
        onChange={setFilters}
        kindLabel="Resultado"
        kindOptions={RISK_RESULTS}
      />
      {error && <p className="error-message">{error.message}</p>}
      <HistoryTable
        columns={RISK_COLUMNS}
        rows={items}
        rowKey={(row) => row.id}
        onSelect={setSelected}
        selectedKey={selected?.id ?? null}
        total={total}
        offset={offset}
        onOffsetChange={setOffset}
        loading={loading}
        emptyMessage="No hay validaciones para estos filtros."
      />
      {selected && (
        <RiskValidationDetail validation={selected} onClose={() => setSelected(null)} />
      )}
    </>
  )
}

export function History({ onUnauthorized }: HistoryProps) {
  const [tab, setTab] = useState<Tab>('decisions')

  return (
    <div className="history-page">
      <div className="tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'decisions'}
          className={tab === 'decisions' ? 'tab tab-active' : 'tab'}
          onClick={() => setTab('decisions')}
        >
          Decisiones
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'blocks'}
          className={tab === 'blocks' ? 'tab tab-active' : 'tab'}
          onClick={() => setTab('blocks')}
        >
          Bloqueos
        </button>
      </div>

      {/* key por pestaña: montar de nuevo resetea filtros, página y selección. */}
      {tab === 'decisions' ? (
        <DecisionsTab key="decisions" onUnauthorized={onUnauthorized} />
      ) : (
        <BlocksTab key="blocks" onUnauthorized={onUnauthorized} />
      )}
    </div>
  )
}
