import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { HistoryFilters } from './HistoryFilters'
import { EMPTY_FILTERS, type HistoryFiltersState } from '../hooks/useHistory'

function renderFilters(filters: HistoryFiltersState = EMPTY_FILTERS) {
  const onChange = vi.fn()
  render(
    <HistoryFilters
      filters={filters}
      onChange={onChange}
      kindLabel="Acción"
      kindOptions={['OPEN', 'NO_OPERAR']}
    />,
  )
  return { onChange }
}

describe('HistoryFilters', () => {
  it('emits the full filter state when the symbol changes', async () => {
    const { onChange } = renderFilters()

    await userEvent.selectOptions(screen.getByLabelText('Símbolo'), 'ETHUSDT')

    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_FILTERS, symbol: 'ETHUSDT' })
  })

  it('emits the kind filter under the label given by the tab', async () => {
    const { onChange } = renderFilters()

    await userEvent.selectOptions(screen.getByLabelText('Acción'), 'NO_OPERAR')

    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_FILTERS, kind: 'NO_OPERAR' })
  })

  it('offers only the five allowed symbols plus "Todos"', () => {
    renderFilters()

    const options = screen.getByLabelText('Símbolo').querySelectorAll('option')
    expect([...options].map((option) => option.textContent)).toEqual([
      'Todos',
      'BTCUSDT',
      'ETHUSDT',
      'BNBUSDT',
      'SOLUSDT',
      'XRPUSDT',
    ])
  })

  it('keeps the selected values shown', () => {
    renderFilters({ from: '2026-03-01T10:00', to: '', symbol: 'SOLUSDT', kind: 'OPEN' })

    expect(screen.getByLabelText('Desde')).toHaveValue('2026-03-01T10:00')
    expect(screen.getByLabelText('Símbolo')).toHaveValue('SOLUSDT')
  })

  it('warns when the range starts after it ends', () => {
    renderFilters({ from: '2026-03-05T10:00', to: '2026-03-01T10:00', symbol: '', kind: '' })

    expect(screen.getByRole('alert')).toHaveTextContent('El inicio del rango es posterior al fin.')
  })

  it('does not warn on a valid range', () => {
    renderFilters({ from: '2026-03-01T10:00', to: '2026-03-05T10:00', symbol: '', kind: '' })

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
