import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { DetailPanel, DetailRow } from './DetailPanel'

describe('DetailPanel', () => {
  it('renders the title and children', () => {
    render(
      <DetailPanel title="Detalle" onClose={vi.fn()}>
        <DetailRow label="Símbolo" value="BTCUSDT" />
      </DetailPanel>,
    )

    expect(screen.getByRole('heading', { name: 'Detalle' })).toBeInTheDocument()
    expect(screen.getByText('Símbolo')).toBeInTheDocument()
    expect(screen.getByText('BTCUSDT')).toBeInTheDocument()
  })

  it('calls onClose when the close button is clicked', async () => {
    const onClose = vi.fn()
    render(
      <DetailPanel title="Detalle" onClose={onClose}>
        <DetailRow label="Símbolo" value="BTCUSDT" />
      </DetailPanel>,
    )

    await userEvent.click(screen.getByRole('button', { name: 'Cerrar' }))

    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('DetailRow', () => {
  it.each([null, undefined, ''])('renders an em dash for %p', (value) => {
    render(<DetailRow label="Motivo" value={value} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('renders zero as-is instead of the em dash fallback', () => {
    render(<DetailRow label="Cantidad" value={0} />)
    expect(screen.getByText('0')).toBeInTheDocument()
    expect(screen.queryByText('—')).not.toBeInTheDocument()
  })
})
