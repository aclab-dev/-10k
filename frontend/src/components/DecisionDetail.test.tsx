import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DecisionDetail } from './DecisionDetail'
import { useDecisionDetail } from '../hooks/useDecisionDetail'
import { ApiError, type DecisionOut } from '../api/client'

vi.mock('../hooks/useDecisionDetail')

const mockedUseDecisionDetail = vi.mocked(useDecisionDetail)

const sampleDecision: DecisionOut = {
  id: 'dec-1',
  bot_run_id: 'run-1',
  symbol: 'BTCUSDT',
  timestamp: '2026-03-02T12:00:00Z',
  action: 'OPEN',
  direction: 'LONG',
  confidence: 0.8123,
  margin_usdt: '10.00000000',
  leverage: 3,
  stop_loss: '61000.00000000',
  take_profit: '65000.00000000',
  reasoning: 'Momentum alcista confirmado',
}

describe('DecisionDetail', () => {
  afterEach(() => {
    mockedUseDecisionDetail.mockReset()
  })

  it('shows a loading state while fetching', () => {
    mockedUseDecisionDetail.mockReturnValue({ decision: null, loading: true, error: null })
    render(<DecisionDetail decisionId="dec-1" onClose={vi.fn()} />)

    expect(screen.getByText('Cargando…')).toBeInTheDocument()
  })

  it('renders the decision fields once loaded', () => {
    mockedUseDecisionDetail.mockReturnValue({
      decision: sampleDecision,
      loading: false,
      error: null,
    })
    render(<DecisionDetail decisionId="dec-1" onClose={vi.fn()} />)

    expect(screen.getByText('dec-1')).toBeInTheDocument()
    expect(screen.getByText('LONG')).toBeInTheDocument()
    expect(screen.getByText('0.81')).toBeInTheDocument()
    expect(screen.getByText('Momentum alcista confirmado')).toBeInTheDocument()
  })

  it('renders an em dash for null fields instead of blanks', () => {
    mockedUseDecisionDetail.mockReturnValue({
      decision: { ...sampleDecision, direction: null, leverage: null, reasoning: null },
      loading: false,
      error: null,
    })
    render(<DecisionDetail decisionId="dec-1" onClose={vi.fn()} />)

    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3)
  })

  it('shows the API error message when the fetch fails', () => {
    mockedUseDecisionDetail.mockReturnValue({
      decision: null,
      loading: false,
      error: new ApiError(404, "decision_id 'dec-1' no encontrada"),
    })
    render(<DecisionDetail decisionId="dec-1" onClose={vi.fn()} />)

    expect(screen.getByText("decision_id 'dec-1' no encontrada")).toBeInTheDocument()
  })

  it('closes the panel', async () => {
    mockedUseDecisionDetail.mockReturnValue({
      decision: sampleDecision,
      loading: false,
      error: null,
    })
    const onClose = vi.fn()
    render(<DecisionDetail decisionId="dec-1" onClose={onClose} />)

    await userEvent.click(screen.getByRole('button', { name: 'Cerrar' }))

    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
