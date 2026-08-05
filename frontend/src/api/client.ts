const TOKEN_KEY = 'dashboard_access_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(path, { ...init, headers })

  if (response.status === 401) {
    clearToken()
    throw new ApiError(401, 'Sesión expirada, iniciá sesión de nuevo')
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const message =
      (body && typeof body.detail === 'string' && body.detail) ||
      `Error ${response.status}`
    throw new ApiError(response.status, message)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return response.json() as Promise<T>
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_at: string
}

export interface SessionOut {
  username: string
  issued_at: string
  expires_at: string
}

export interface AccountStateOut {
  timestamp: string
  balance_usdt: string
  equity_usdt: string
  margin_used_usdt: string
  unrealized_pnl: string
  realized_pnl_session: string
  drawdown_percent: number
  exposure_percent: number
}

export interface BotStatusOut {
  bot_run_id: string
  environment: string
  app_version: string
  run_status: string
  started_at: string
  ended_at: string | null
  state: string | null
  previous_state: string | null
  state_reason: string | null
  state_updated_at: string | null
  account: AccountStateOut | null
}

export interface KillSwitchOut {
  bot_run_id: string
  state: string
  previous_state: string
  reason: string
  triggered_at: string
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
}

export async function getMe(): Promise<SessionOut> {
  return request<SessionOut>('/api/auth/me')
}

export async function getStatus(): Promise<BotStatusOut> {
  return request<BotStatusOut>('/api/status')
}

export async function triggerKillSwitch(reason: string): Promise<KillSwitchOut> {
  return request<KillSwitchOut>('/api/kill-switch', {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}
