import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Login } from './Login'
import { ApiError, getToken, login } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, login: vi.fn() }
})

const mockedLogin = vi.mocked(login)

describe('Login', () => {
  afterEach(() => {
    mockedLogin.mockReset()
  })

  it('calls onLoggedIn and stores the token on success', async () => {
    mockedLogin.mockResolvedValue({
      access_token: 'a-token',
      token_type: 'bearer',
      expires_at: new Date().toISOString(),
    })
    const onLoggedIn = vi.fn()
    const user = userEvent.setup()
    render(<Login onLoggedIn={onLoggedIn} />)

    await user.type(screen.getByLabelText('Usuario'), 'operador')
    await user.type(screen.getByLabelText('Contraseña'), 'secreto')
    await user.click(screen.getByRole('button', { name: 'Ingresar' }))

    await waitFor(() => expect(onLoggedIn).toHaveBeenCalledTimes(1))
    expect(mockedLogin).toHaveBeenCalledWith('operador', 'secreto')
    expect(getToken()).toBe('a-token')
  })

  it('shows the API error message and does not call onLoggedIn on failure', async () => {
    mockedLogin.mockRejectedValue(new ApiError(401, 'Credenciales inválidas'))
    const onLoggedIn = vi.fn()
    const user = userEvent.setup()
    render(<Login onLoggedIn={onLoggedIn} />)

    await user.type(screen.getByLabelText('Usuario'), 'operador')
    await user.type(screen.getByLabelText('Contraseña'), 'incorrecta')
    await user.click(screen.getByRole('button', { name: 'Ingresar' }))

    expect(await screen.findByText('Credenciales inválidas')).toBeInTheDocument()
    expect(onLoggedIn).not.toHaveBeenCalled()
  })

  it('disables the submit button while submitting', async () => {
    let resolveLogin: (() => void) | undefined
    mockedLogin.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLogin = () =>
            resolve({
              access_token: 'a-token',
              token_type: 'bearer',
              expires_at: new Date().toISOString(),
            })
        }),
    )
    const user = userEvent.setup()
    render(<Login onLoggedIn={vi.fn()} />)

    await user.type(screen.getByLabelText('Usuario'), 'operador')
    await user.type(screen.getByLabelText('Contraseña'), 'secreto')
    await user.click(screen.getByRole('button', { name: 'Ingresar' }))

    expect(await screen.findByRole('button', { name: 'Ingresando…' })).toBeDisabled()

    resolveLogin?.()
  })
})
