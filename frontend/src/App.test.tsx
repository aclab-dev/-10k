import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { ApiError, getMe } from './api/client'

vi.mock('./api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api/client')>()
  return { ...actual, getMe: vi.fn() }
})

vi.mock('./pages/Status', () => ({
  Status: () => <div>vista de estado</div>,
}))

vi.mock('./pages/History', () => ({
  History: () => <div>vista de historial</div>,
}))

const mockedGetMe = vi.mocked(getMe)

describe('App navigation', () => {
  beforeEach(() => {
    mockedGetMe.mockResolvedValue({
      username: 'admin',
      issued_at: '2026-03-02T12:00:00Z',
      expires_at: '2026-03-03T12:00:00Z',
    })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('lands on the status view once authenticated', async () => {
    render(<App />)

    expect(await screen.findByText('vista de estado')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Estado' })).toHaveAttribute('aria-current', 'page')
  })

  it('switches to the history view and back', async () => {
    render(<App />)
    await screen.findByText('vista de estado')

    await userEvent.click(screen.getByRole('button', { name: 'Historial' }))
    expect(screen.getByText('vista de historial')).toBeInTheDocument()
    expect(screen.queryByText('vista de estado')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Estado' }))
    expect(screen.getByText('vista de estado')).toBeInTheDocument()
  })

  it('shows the login form and no nav when the session check fails', async () => {
    mockedGetMe.mockRejectedValue(new ApiError(401, 'Sesión expirada'))
    render(<App />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Ingresar' })).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Historial' })).not.toBeInTheDocument()
  })
})
