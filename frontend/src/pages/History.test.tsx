import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { History } from './History'
import {
  ApiError,
  getDecision,
  getDecisions,
  getRiskValidations,
  type DecisionOut,
  type Page,
  type RiskValidationOut,
} from '../api/client'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    getDecisions: vi.fn(),
    getDecision: vi.fn(),
    getRiskValidations: vi.fn(),
  }
})

const mockedGetDecisions = vi.mocked(getDecisions)
const mockedGetDecision = vi.mocked(getDecision)
const mockedGetRiskValidations = vi.mocked(getRiskValidations)

const decision: DecisionOut = {
  id: 'dec-1',
  bot_run_id: 'run-1',
  symbol: 'BTCUSDT',
  timestamp: '2026-03-02T12:00:00Z',
  action: 'OPEN',
  direction: 'LONG',
  confidence: 0.8,
  margin_usdt: '10.00000000',
  leverage: 3,
  stop_loss: '61000.00000000',
  take_profit: '65000.00000000',
  reasoning: 'Momentum alcista',
}

const validation: RiskValidationOut = {
  id: 'rv-1',
  bot_run_id: 'run-1',
  symbol: 'ETHUSDT',
  timestamp: '2026-03-02T12:00:00Z',
  result: 'BLOCK',
  original_margin: '25.00000000',
  original_leverage: 12,
  adjusted_margin: null,
  adjusted_leverage: null,
  reasons: { rule: 'max_margin_exceeded' },
  daily_loss_at_check: null,
  total_loss_at_check: null,
}

function page<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 25, offset: 0 }
}

describe('History', () => {
  beforeEach(() => {
    mockedGetDecisions.mockResolvedValue(page([decision]))
    mockedGetRiskValidations.mockResolvedValue(page([validation]))
    mockedGetDecision.mockResolvedValue(decision)
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('opens on the decisions tab and lists them', async () => {
    render(<History onUnauthorized={vi.fn()} />)

    await waitFor(() => expect(screen.getByText('OPEN')).toBeInTheDocument())
    expect(screen.getByRole('tab', { name: 'Decisiones' })).toHaveAttribute('aria-selected', 'true')
  })

  it('defaults the blocks tab to result=BLOCK', async () => {
    render(<History onUnauthorized={vi.fn()} />)

    await userEvent.click(screen.getByRole('tab', { name: 'Bloqueos' }))

    await waitFor(() =>
      expect(mockedGetRiskValidations).toHaveBeenCalledWith(
        expect.objectContaining({ result: 'BLOCK' }),
      ),
    )
    expect(screen.getByLabelText('Resultado')).toHaveValue('BLOCK')
  })

  it('sends the symbol filter to the backend', async () => {
    render(<History onUnauthorized={vi.fn()} />)
    await waitFor(() => expect(mockedGetDecisions).toHaveBeenCalled())

    await userEvent.selectOptions(screen.getByLabelText('Símbolo'), 'SOLUSDT')

    await waitFor(() =>
      expect(mockedGetDecisions).toHaveBeenLastCalledWith(
        expect.objectContaining({ symbol: 'SOLUSDT' }),
      ),
    )
  })

  it('drills down into a decision through the detail endpoint', async () => {
    render(<History onUnauthorized={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('OPEN')).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: /2026/ }))

    await waitFor(() => expect(mockedGetDecision).toHaveBeenCalledWith('dec-1'))
    expect(await screen.findByLabelText('Detalle de la decisión')).toBeInTheDocument()
    expect(screen.getByText('Momentum alcista')).toBeInTheDocument()
  })

  it('drills down into a block without calling a detail endpoint', async () => {
    render(<History onUnauthorized={vi.fn()} />)
    await userEvent.click(screen.getByRole('tab', { name: 'Bloqueos' }))
    // 'BLOCK' también es una <option> del filtro: se busca la celda de la fila.
    await waitFor(() => expect(screen.getByRole('cell', { name: 'BLOCK' })).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: /2026/ }))

    expect(await screen.findByLabelText('Detalle de la validación')).toBeInTheDocument()
    expect(screen.getByText(/max_margin_exceeded/)).toBeInTheDocument()
  })

  it('resets filters and selection when switching tabs', async () => {
    render(<History onUnauthorized={vi.fn()} />)
    await waitFor(() => expect(mockedGetDecisions).toHaveBeenCalled())
    await userEvent.selectOptions(screen.getByLabelText('Símbolo'), 'SOLUSDT')

    await userEvent.click(screen.getByRole('tab', { name: 'Bloqueos' }))
    await userEvent.click(screen.getByRole('tab', { name: 'Decisiones' }))

    await waitFor(() => expect(screen.getByLabelText('Símbolo')).toHaveValue(''))
    expect(mockedGetDecisions).toHaveBeenLastCalledWith(
      expect.objectContaining({ symbol: undefined, offset: 0 }),
    )
  })

  it('shows the error message when the listing fails', async () => {
    mockedGetDecisions.mockRejectedValue(new ApiError(404, 'No hay ningún bot run activo'))
    render(<History onUnauthorized={vi.fn()} />)

    expect(await screen.findByText('No hay ningún bot run activo')).toBeInTheDocument()
  })

  it('reports a 401 upwards so the app can log out', async () => {
    mockedGetDecisions.mockRejectedValue(new ApiError(401, 'Sesión expirada'))
    const onUnauthorized = vi.fn()
    render(<History onUnauthorized={onUnauthorized} />)

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalled())
  })
})
