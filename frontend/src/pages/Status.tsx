import { useStatus } from '../hooks/useStatus'
import { KillSwitchButton } from '../components/KillSwitchButton'

const STATE_COLOR: Record<string, string> = {
  ACTIVE: 'green',
  SAFE_MODE: 'yellow',
  MANUAL_PAUSED: 'yellow',
  HALTED: 'red',
  KILL_SWITCH_TRIGGERED: 'red',
}

function StateBadge({ state }: { state: string | null }) {
  if (!state) {
    return <span className="badge badge-unknown">Sin estado</span>
  }
  const color = STATE_COLOR[state] ?? 'unknown'
  return <span className={`badge badge-${color}`}>{state}</span>
}

function formatUsd(value: string | undefined): string {
  if (value === undefined) return '—'
  const n = Number(value)
  return Number.isFinite(n) ? n.toLocaleString('en-US', { maximumFractionDigits: 2 }) : value
}

interface StatusProps {
  onLoggedOut: () => void
}

export function Status({ onLoggedOut }: StatusProps) {
  const { status, error, loading, refresh } = useStatus()

  if (error?.status === 401) {
    onLoggedOut()
    return null
  }

  if (loading && !status) {
    return <div className="status-page">Cargando estado del bot…</div>
  }

  if (error?.status === 404) {
    return (
      <div className="status-page">
        <p>No hay ningún bot run activo en este momento.</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="status-page">
        <p className="error-message">{error.message}</p>
      </div>
    )
  }

  const account = status?.account ?? null

  return (
    <div className="status-page">
      <header className="status-header">
        <div>
          <h1>Estado del bot</h1>
          <p className="environment">
            {status?.environment} · {status?.app_version} · run {status?.run_status}
          </p>
        </div>
        <KillSwitchButton currentState={status?.state ?? null} onTriggered={refresh} />
      </header>

      <section className="state-section">
        <StateBadge state={status?.state ?? null} />
        {status?.state_reason && <p className="state-reason">{status.state_reason}</p>}
        {status?.previous_state && (
          <p className="previous-state">Estado anterior: {status.previous_state}</p>
        )}
      </section>

      <section className="metrics-grid">
        <div className="metric-card">
          <span className="metric-label">Equity (USDT)</span>
          <span className="metric-value">{formatUsd(account?.equity_usdt)}</span>
        </div>
        <div className="metric-card">
          <span className="metric-label">PnL no realizado</span>
          <span className="metric-value">{formatUsd(account?.unrealized_pnl)}</span>
        </div>
        <div className="metric-card">
          <span className="metric-label">PnL realizado (sesión)</span>
          <span className="metric-value">{formatUsd(account?.realized_pnl_session)}</span>
        </div>
        <div className="metric-card">
          <span className="metric-label">Drawdown</span>
          <span className="metric-value">{account ? `${account.drawdown_percent}%` : '—'}</span>
        </div>
        <div className="metric-card">
          <span className="metric-label">Exposición</span>
          <span className="metric-value">{account ? `${account.exposure_percent}%` : '—'}</span>
        </div>
      </section>

      {!account && <p className="empty-hint">Todavía no hay snapshot de cuenta para este run.</p>}
    </div>
  )
}
