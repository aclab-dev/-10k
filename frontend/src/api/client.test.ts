import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, clearToken, getStatus, setToken } from './client'

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
