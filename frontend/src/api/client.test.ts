import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  clearToken,
  getDecision,
  getDecisions,
  getRiskValidations,
  getStatus,
  setToken,
} from './client'

describe('request (via client functions)', () => {
  beforeEach(() => {
    setToken('a-token')
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    clearToken()
    vi.unstubAllGlobals()
  })

  it('clears the token and throws ApiError on a 401 response', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(null, { status: 401 }) as unknown as Response,
    )

    await expect(getStatus()).rejects.toThrow(ApiError)
    expect(localStorage.getItem('dashboard_access_token')).toBeNull()
  })

  it('does not send Content-Type on GET requests', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200 }) as unknown as Response,
    )

    await getStatus()

    const [, init] = vi.mocked(fetch).mock.calls[0]
    const headers = new Headers(init?.headers)
    expect(headers.has('Content-Type')).toBe(false)
  })
})

describe('history endpoints', () => {
  beforeEach(() => {
    setToken('a-token')
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, limit: 25, offset: 0 }), {
        status: 200,
      }) as unknown as Response,
    )
  })

  afterEach(() => {
    clearToken()
    vi.unstubAllGlobals()
  })

  function requestedUrl(): string {
    return String(vi.mocked(fetch).mock.calls[0][0])
  }

  it('serialises the decision filters into the query string', async () => {
    await getDecisions({
      symbol: 'BTCUSDT',
      action: 'OPEN',
      from_ts: '2026-03-01T00:00:00.000Z',
      to_ts: '2026-03-05T00:00:00.000Z',
      limit: 25,
      offset: 50,
    })

    const url = new URL(requestedUrl(), 'http://localhost')
    expect(url.pathname).toBe('/api/decisions')
    expect(url.searchParams.get('symbol')).toBe('BTCUSDT')
    expect(url.searchParams.get('action')).toBe('OPEN')
    expect(url.searchParams.get('from_ts')).toBe('2026-03-01T00:00:00.000Z')
    expect(url.searchParams.get('offset')).toBe('50')
  })

  it('omits undefined and empty filters from the query string', async () => {
    await getDecisions({ symbol: undefined, action: '', limit: 25, offset: 0 })

    const url = new URL(requestedUrl(), 'http://localhost')
    expect(url.searchParams.has('symbol')).toBe(false)
    expect(url.searchParams.has('action')).toBe(false)
    expect(url.searchParams.get('offset')).toBe('0')
  })

  it('sends the risk filters to the validations endpoint', async () => {
    await getRiskValidations({ result: 'BLOCK', limit: 25, offset: 0 })

    const url = new URL(requestedUrl(), 'http://localhost')
    expect(url.pathname).toBe('/api/risk/validations')
    expect(url.searchParams.get('result')).toBe('BLOCK')
  })

  it('encodes the decision id in the detail path', async () => {
    await getDecision('dec/1')

    expect(requestedUrl()).toBe('/api/decisions/dec%2F1')
  })
})
