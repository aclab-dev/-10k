import { useState, type FormEvent } from 'react'
import { ApiError, login, setToken } from '../api/client'

interface LoginProps {
  onLoggedIn: () => void
}

export function Login({ onLoggedIn }: LoginProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      const response = await login(username, password)
      setToken(response.access_token)
      onLoggedIn()
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Auth deshabilitada server-side (dashboard_auth.enabled=false): no
        // hay login que hacer, seguimos directo al dashboard.
        onLoggedIn()
        return
      }
      setError(err instanceof ApiError ? err.message : 'Error inesperado')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="login-page">
      <form className="login-form" onSubmit={handleSubmit}>
        <h1>-10k Dashboard</h1>
        <label>
          Usuario
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label>
          Contraseña
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && <p className="error-message">{error}</p>}
        <button type="submit" disabled={submitting}>
          {submitting ? 'Ingresando…' : 'Ingresar'}
        </button>
      </form>
    </div>
  )
}
