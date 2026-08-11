import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Status } from './Status'
import { useStatus } from '../hooks/useStatus'
import { ApiError, type BotStatusOut } from '../api/client'

vi.mock('../hooks/useStatus')

const mockedUseStatus = vi.mocked(useStatus)

const sampleStatus: BotStatusOut = {
  bot_run_id: 'run-1',
  environment: 'PAPER',
  app_version: '0.1.0',
  run_status: 'RUNNING',
  started_at: new Date().toISOString(),
  ended_at: null,
  state: 'ACTIVE',
  previous_state: null,
  state_reason: null,
  state_updated_at: null,
  account: null,
}

describe('Status', () => {
  afterEach(() => {
    mockedUseStatus.mockReset()
  })

  it('shows a loading message while there is no status yet', () => {
    mockedUseStatus.mockReturnValue({
      status: null,
      error: null,
      loading: true,
      refresh: vi.fn(),
    })
    render(<Status onLoggedOut={vi.fn()} />)

    expect(screen.getByText('Cargando estado del bot…')).toBeInTheDocument()
  })

  it('renders the bot state and metrics once loaded', () => {
    mockedUseStatus.mockReturnValue({
      status: sampleStatus,
      error: null,
      loading: false,
      refresh: vi.fn(),
    })
    render(<Status onLoggedOut={vi.fn()} />)

    expect(screen.getByText('ACTIVE')).toBeInTheDocument()
    expect(screen.getByText(/PAPER/)).toBeInTheDocument()
  })

  it('shows the empty-run message on a 404 error', () => {
    mockedUseStatus.mockReturnValue({
      status: null,
      error: new ApiError(404, 'No hay ningún bot run activo'),
      loading: false,
      refresh: vi.fn(),
    })
    render(<Status onLoggedOut={vi.fn()} />)

    expect(screen.getByText('No hay ningún bot run activo en este momento.')).toBeInTheDocument()
  })

  it('calls onLoggedOut from an effect (not during render) on a 401 error', async () => {
    mockedUseStatus.mockReturnValue({
      status: null,
      error: new ApiError(401, 'Sesión expirada'),
      loading: false,
      refresh: vi.fn(),
    })
    const onLoggedOut = vi.fn()
    const { container } = render(<Status onLoggedOut={onLoggedOut} />)

    await waitFor(() => expect(onLoggedOut).toHaveBeenCalledTimes(1))
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the generic error message for other errors', () => {
    mockedUseStatus.mockReturnValue({
      status: null,
      error: new ApiError(500, 'Error inesperado'),
      loading: false,
      refresh: vi.fn(),
    })
    render(<Status onLoggedOut={vi.fn()} />)

    expect(screen.getByText('Error inesperado')).toBeInTheDocument()
  })
})
