import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useStatus } from './useStatus'
import { ApiError, getStatus, type BotStatusOut } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, getStatus: vi.fn() }
})

const mockedGetStatus = vi.mocked(getStatus)

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

describe('useStatus', () => {
  beforeEach(() => {
    // shouldAdvanceTime deja que `waitFor` (que usa setTimeout real por
    // debajo) siga funcionando mientras el hook corre con fake timers, para
    // poder despues avanzar el reloj manualmente y probar el polling.
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    mockedGetStatus.mockReset()
    vi.useRealTimers()
  })

  it('fetches status on mount and exposes it once loaded', async () => {
    mockedGetStatus.mockResolvedValue(sampleStatus)
    const { result } = renderHook(() => useStatus())

    expect(result.current.loading).toBe(true)

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.status).toEqual(sampleStatus)
    expect(result.current.error).toBeNull()
  })

  it('exposes an ApiError when the request fails', async () => {
    mockedGetStatus.mockRejectedValue(new ApiError(404, 'No hay ningún bot run activo'))
    const { result } = renderHook(() => useStatus())

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.error).toBeInstanceOf(ApiError)
    expect(result.current.error?.status).toBe(404)
    expect(result.current.status).toBeNull()
  })

  it('polls again after the interval elapses', async () => {
    mockedGetStatus.mockResolvedValue(sampleStatus)
    renderHook(() => useStatus())

    await waitFor(() => expect(mockedGetStatus).toHaveBeenCalledTimes(1))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })

    expect(mockedGetStatus).toHaveBeenCalledTimes(2)
  })

  it('refetches immediately when refresh() is called', async () => {
    mockedGetStatus.mockResolvedValue(sampleStatus)
    const { result } = renderHook(() => useStatus())

    await waitFor(() => expect(mockedGetStatus).toHaveBeenCalledTimes(1))

    act(() => {
      result.current.refresh()
    })

    await waitFor(() => expect(mockedGetStatus).toHaveBeenCalledTimes(2))
  })
})
