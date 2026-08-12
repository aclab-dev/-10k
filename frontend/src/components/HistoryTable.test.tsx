import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { HistoryTable, type Column } from './HistoryTable'
import { PAGE_SIZE } from '../hooks/useHistory'

interface Row {
  id: string
  symbol: string
}

const columns: Column<Row>[] = [
  { key: 'id', header: 'ID', render: (row) => row.id },
  { key: 'symbol', header: 'Símbolo', render: (row) => row.symbol },
]

function renderTable(props: Partial<Parameters<typeof HistoryTable<Row>>[0]> = {}) {
  const onSelect = vi.fn()
  const onOffsetChange = vi.fn()
  render(
    <HistoryTable
      columns={columns}
      rows={[{ id: 'r1', symbol: 'BTCUSDT' }]}
      rowKey={(row) => row.id}
      onSelect={onSelect}
      selectedKey={null}
      total={1}
      offset={0}
      onOffsetChange={onOffsetChange}
      loading={false}
      emptyMessage="Sin datos."
      {...props}
    />,
  )
  return { onSelect, onOffsetChange }
}

describe('HistoryTable', () => {
  it('renders one row per item with all columns', () => {
    renderTable()

    expect(screen.getByRole('columnheader', { name: 'Símbolo' })).toBeInTheDocument()
    expect(screen.getByText('BTCUSDT')).toBeInTheDocument()
  })

  it('calls onSelect with the clicked row', async () => {
    const { onSelect } = renderTable()

    await userEvent.click(screen.getByRole('button', { name: 'r1' }))

    expect(onSelect).toHaveBeenCalledWith({ id: 'r1', symbol: 'BTCUSDT' })
  })

  it('shows the empty message when there are no rows and it is not loading', () => {
    renderTable({ rows: [], total: 0 })

    expect(screen.getByText('Sin datos.')).toBeInTheDocument()
  })

  it('does not show the empty message while loading', () => {
    renderTable({ rows: [], total: 0, loading: true })

    expect(screen.queryByText('Sin datos.')).not.toBeInTheDocument()
    expect(screen.getByText('Cargando…')).toBeInTheDocument()
  })

  it('disables both pagination buttons on a single full page', () => {
    renderTable()

    expect(screen.getByRole('button', { name: 'Anterior' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Siguiente' })).toBeDisabled()
  })

  it('enables Siguiente while there are more rows than the current page shows', () => {
    renderTable({ total: 100 })

    expect(screen.getByRole('button', { name: 'Siguiente' })).toBeEnabled()
  })

  it('advances the offset by a full page', async () => {
    const { onOffsetChange } = renderTable({ total: 100 })

    await userEvent.click(screen.getByRole('button', { name: 'Siguiente' }))

    expect(onOffsetChange).toHaveBeenCalledWith(PAGE_SIZE)
  })

  it('never goes below offset 0 when going back', async () => {
    const { onOffsetChange } = renderTable({ offset: 10, total: 100 })

    await userEvent.click(screen.getByRole('button', { name: 'Anterior' }))

    expect(onOffsetChange).toHaveBeenCalledWith(0)
  })

  it('shows the human range of the current page', () => {
    renderTable({ offset: 25, total: 100 })

    expect(screen.getByText('26–26 de 100')).toBeInTheDocument()
  })

  it('shows a zero range when there is nothing to page through', () => {
    renderTable({ rows: [], total: 0 })

    expect(screen.getByText('0 de 0')).toBeInTheDocument()
  })
})
