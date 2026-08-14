import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useDecisionDetail } from './useDecisionDetail'
import { ApiError, getDecision, type DecisionOut } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, getDecision: vi.fn() }
})

const mockedGetDecision = vi.mocked(getDecision)

const sampleDecision: DecisionOut = {
  id: 'dec-1',
  bot_run_id: 'run-1',
  symbol: 'BTCUSDT',
  timestamp: '2026-03-02T12:00:00Z',
  action: 'OPEN',
  direction: 'LONG',
  confidence: 0.8,
  margin_usdt: '10.00000000',
  leverage: 3,
  stop_loss: '61000.00000000',
  take_profit: '65000.00000000',
  reasoning: 'Momentum alcista confirmado',
}

describe('useDecisionDetail', () => {
  afterEach(() => {
    mockedGetDecision.mockReset()
  })

  it('starts idle when decisionId is null', () => {
    const { result } = renderHook(() => useDecisionDetail(null))

    expect(result.current.loading).toBe(false)
    expect(result.current.decision).toBeNull()
    expect(result.current.error).toBeNull()
    expect(mockedGetDecision).not.toHaveBeenCalled()
  })

  it('fetches the decision and exposes it once loaded', async () => {
    mockedGetDecision.mockResolvedValue(sampleDecision)
    const { result } = renderHook(() => useDecisionDetail('dec-1'))

    expect(result.current.loading).toBe(true)

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.decision).toEqual(sampleDecision)
    expect(result.current.error).toBeNull()
    expect(mockedGetDecision).toHaveBeenCalledWith('dec-1')
  })

  it('exposes an ApiError as-is when the fetch fails', async () => {
    mockedGetDecision.mockRejectedValue(new ApiError(404, "decision_id 'dec-1' no encontrada"))
    const { result } = renderHook(() => useDecisionDetail('dec-1'))

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.error).toBeInstanceOf(ApiError)
    expect(result.current.error?.status).toBe(404)
    expect(result.current.decision).toBeNull()
  })

  it('wraps a non-ApiError failure into a generic 500 ApiError', async () => {
    mockedGetDecision.mockRejectedValue(new Error('network down'))
    const { result } = renderHook(() => useDecisionDetail('dec-1'))

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.error).toBeInstanceOf(ApiError)
    expect(result.current.error?.status).toBe(500)
  })

  it('resets to idle when decisionId goes back to null', async () => {
    mockedGetDecision.mockResolvedValue(sampleDecision)
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useDecisionDetail(id),
      { initialProps: { id: 'dec-1' as string | null } },
    )

    await waitFor(() => expect(result.current.decision).toEqual(sampleDecision))

    rerender({ id: null })

    expect(result.current.decision).toBeNull()
    expect(result.current.loading).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('ignores a stale response from a superseded decisionId', async () => {
    let resolveFirst: (() => void) | undefined
    mockedGetDecision.mockImplementation(
      (id: string) =>
        new Promise((resolve) => {
          if (id === 'dec-1') {
            resolveFirst = () => resolve(sampleDecision)
          } else {
            resolve({ ...sampleDecision, id: 'dec-2' })
          }
        }),
    )

    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useDecisionDetail(id),
      { initialProps: { id: 'dec-1' } },
    )

    rerender({ id: 'dec-2' })

    await waitFor(() => expect(result.current.decision?.id).toBe('dec-2'))

    // Si la respuesta de dec-1 pisara el estado tras la de dec-2, decision volvería a dec-1.
    resolveFirst?.()

    expect(result.current.decision?.id).toBe('dec-2')
  })
})
