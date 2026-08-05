import { useEffect, useState } from 'react'
import { ApiError, clearToken, getMe } from './api/client'
import { Login } from './pages/Login'
import { Status } from './pages/Status'

type AuthState = 'checking' | 'authed' | 'unauthed'

function App() {
  const [auth, setAuth] = useState<AuthState>('checking')

  useEffect(() => {
    let cancelled = false
    getMe()
      .then(() => {
        if (!cancelled) setAuth('authed')
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ApiError) clearToken()
        setAuth('unauthed')
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (auth === 'checking') {
    return <div className="app-loading">Cargando…</div>
  }

  if (auth === 'unauthed') {
    return <Login onLoggedIn={() => setAuth('authed')} />
  }

  return (
    <Status
      onLoggedOut={() => {
        clearToken()
        setAuth('unauthed')
      }}
    />
  )
}

export default App
