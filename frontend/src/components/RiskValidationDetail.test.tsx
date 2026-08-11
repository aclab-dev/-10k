import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { RiskValidationDetail } from './RiskValidationDetail'
import type { RiskValidationOut } from '../api/client'

const sampleValidation: RiskValidationOut = {
  id: 'rv-1',
  bot_run_id: 'run-1',
  symbol: 'ETHUSDT',
  timestamp: '2026-03-02T12:00:00Z',
  result: 'BLOCK',
  original_margin: '25.00000000',
  original_leverage: 12,
  adjusted_margin: null,
  adjusted_leverage: null,
  reasons: { rule: 'max_margin_exceeded', limit: '10' },
  daily_loss_at_check: '-3.20000000',
  total_loss_at_check: '-8.40000000',
}

describe('RiskValidationDetail', () => {
  it('renders the validation fields from the row without refetching', () => {
    render(<RiskValidationDetail validation={sampleValidation} onClose={vi.fn()} />)

    expect(screen.getByText('rv-1')).toBeInTheDocument()
    expect(screen.getByText('BLOCK')).toBeInTheDocument()
    expect(screen.getByText('25.00000000')).toBeInTheDocument()
  })

  it('renders the reasons payload as readable JSON', () => {
    render(<RiskValidationDetail validation={sampleValidation} onClose={vi.fn()} />)

    expect(screen.getByText(/max_margin_exceeded/)).toBeInTheDocument()
  })

  it('renders an em dash when there are no reasons', () => {
    render(
      <RiskValidationDetail
        validation={{ ...sampleValidation, result: 'APPROVE', reasons: null }}
        onClose={vi.fn()}
      />,
    )

    expect(screen.queryByText(/max_margin_exceeded/)).not.toBeInTheDocument()
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(1)
  })

  it('closes the panel', async () => {
    const onClose = vi.fn()
    render(<RiskValidationDetail validation={sampleValidation} onClose={onClose} />)

    await userEvent.click(screen.getByRole('button', { name: 'Cerrar' }))

    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
