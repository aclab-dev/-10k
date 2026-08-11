import { useCallback, useEffect, useState } from 'react'
import { ApiError, clearToken, getMe } from './api/client'
import { History } from './pages/History'
import { Login } from './pages/Login'
import { Status } from './pages/Status'

type AuthState = 'checking' | 'authed' | 'unauthed'
type View = 'status' | 'history'

function App() {
  const [auth, setAuth] = useState<AuthState>('checking')
  const [view, setView] = useState<View>('status')

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

  const logout = useCallback(() => {
    clearToken()
    setAuth('unauthed')
  }, [])

  if (auth === 'checking') {
    return <div className="app-loading">Cargando…</div>
  }

  if (auth === 'unauthed') {
    return <Login onLoggedIn={() => setAuth('authed')} />
  }

  return (
    <>
      <nav className="app-nav" aria-label="Secciones">
        <button
          type="button"
          className={view === 'status' ? 'nav-link nav-link-active' : 'nav-link'}
          aria-current={view === 'status' ? 'page' : undefined}
          onClick={() => setView('status')}
        >
          Estado
        </button>
        <button
          type="button"
          className={view === 'history' ? 'nav-link nav-link-active' : 'nav-link'}
          aria-current={view === 'history' ? 'page' : undefined}
          onClick={() => setView('history')}
        >
          Historial
        </button>
      </nav>

      {view === 'status' ? (
        <Status onLoggedOut={logout} />
      ) : (
        <History onUnauthorized={logout} />
      )}
    </>
  )
}

export default App
