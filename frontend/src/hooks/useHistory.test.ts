import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type HistoryQuery, type Page } from '../api/client'
import { EMPTY_FILTERS, PAGE_SIZE, toIsoUtc, useHistory } from './useHistory'

function pageOf(items: string[], total: number, offset = 0): Page<string> {
  return { items, total, limit: PAGE_SIZE, offset }
}

describe('toIsoUtc', () => {
  it('returns undefined for an empty value', () => {
    expect(toIsoUtc('')).toBeUndefined()
  })

  it('returns undefined for an unparseable value', () => {
    expect(toIsoUtc('ayer')).toBeUndefined()
  })

  it('converts a local datetime-local value to a UTC instant', () => {
    const iso = toIsoUtc('2026-03-02T14:00')
    // El offset del runner es desconocido, pero el instante debe coincidir con
    // el que produce el propio Date para esa hora local.
    expect(iso).toBe(new Date(2026, 2, 2, 14, 0).toISOString())
  })
})

describe('useHistory', () => {
  it('loads the first page and exposes items and total', async () => {
    const fetchPage = vi.fn<(query: HistoryQuery) => Promise<Page<string>>>().mockResolvedValue(
      pageOf(['a', 'b'], 2),
    )

    const { result } = renderHook(() => useHistory(fetchPage, EMPTY_FILTERS, 'action'))

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.items).toEqual(['a', 'b'])
    expect(result.current.total).toBe(2)
  })

  it('sends the filters as backend query params', async () => {
    const fetchPage = vi.fn<(query: HistoryQuery) => Promise<Page<string>>>().mockResolvedValue(
      pageOf([], 0),
    )
    const filters = { from: '', to: '', symbol: 'ETHUSDT', kind: 'OPEN' }

    renderHook(() => useHistory(fetchPage, filters, 'action'))

    await waitFor(() => expect(fetchPage).toHaveBeenCalled())
    expect(fetchPage).toHaveBeenCalledWith(
      expect.objectContaining({ symbol: 'ETHUSDT', action: 'OPEN', offset: 0, limit: PAGE_SIZE }),
    )
  })

  it('names the kind param `result` for the blocks tab', async () => {
    const fetchPage = vi.fn<(query: HistoryQuery) => Promise<Page<string>>>().mockResolvedValue(
      pageOf([], 0),
    )
    const filters = { ...EMPTY_FILTERS, kind: 'BLOCK' }

    renderHook(() => useHistory(fetchPage, filters, 'result'))

    await waitFor(() => expect(fetchPage).toHaveBeenCalled())
    expect(fetchPage).toHaveBeenCalledWith(expect.objectContaining({ result: 'BLOCK' }))
  })

  it('omits empty filters instead of sending blank values', async () => {
    const fetchPage = vi.fn<(query: HistoryQuery) => Promise<Page<string>>>().mockResolvedValue(
      pageOf([], 0),
    )

    renderHook(() => useHistory(fetchPage, EMPTY_FILTERS, 'action'))

    await waitFor(() => expect(fetchPage).toHaveBeenCalled())
    const query = fetchPage.mock.calls[0][0]
    expect(query.symbol).toBeUndefined()
    expect(query.from_ts).toBeUndefined()
    expect(query.to_ts).toBeUndefined()
  })

  it('refetches with the new offset when the page changes', async () => {
    const fetchPage = vi.fn<(query: HistoryQuery) => Promise<Page<string>>>().mockResolvedValue(
      pageOf(['a'], 100),
    )
    const { result } = renderHook(() => useHistory(fetchPage, EMPTY_FILTERS, 'action'))

    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.setOffset(PAGE_SIZE))

    await waitFor(() =>
      expect(fetchPage).toHaveBeenLastCalledWith(expect.objectContaining({ offset: PAGE_SIZE })),
    )
  })

  it('resets the offset to 0 when the filters change', async () => {
    const fetchPage = vi.fn<(query: HistoryQuery) => Promise<Page<string>>>().mockResolvedValue(
      pageOf(['a'], 100),
    )
    const { result, rerender } = renderHook(
      ({ filters }) => useHistory(fetchPage, filters, 'action'),
      { initialProps: { filters: EMPTY_FILTERS } },
    )

    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => result.current.setOffset(PAGE_SIZE * 2))
    await waitFor(() => expect(result.current.offset).toBe(PAGE_SIZE * 2))

    rerender({ filters: { ...EMPTY_FILTERS, symbol: 'BTCUSDT' } })

    await waitFor(() => expect(result.current.offset).toBe(0))
    expect(fetchPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 0, symbol: 'BTCUSDT' }),
    )
  })

  it('exposes the ApiError when the request fails', async () => {
    const fetchPage = vi
      .fn<(query: HistoryQuery) => Promise<Page<string>>>()
      .mockRejectedValue(new ApiError(422, 'rango inválido'))

    const { result } = renderHook(() => useHistory(fetchPage, EMPTY_FILTERS, 'action'))

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.error?.status).toBe(422)
    expect(result.current.items).toEqual([])
  })

  it('discards the previous page when a later request fails', async () => {
    const fetchPage = vi
      .fn<(query: HistoryQuery) => Promise<Page<string>>>()
      .mockResolvedValueOnce(pageOf(['a'], 100))
      .mockRejectedValueOnce(new ApiError(422, 'rango inválido'))
    const { result } = renderHook(() => useHistory(fetchPage, EMPTY_FILTERS, 'action'))

    await waitFor(() => expect(result.current.items).toEqual(['a']))
    act(() => result.current.setOffset(PAGE_SIZE))

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.items).toEqual([])
    expect(result.current.total).toBe(0)
  })

  it('wraps a non-ApiError rejection instead of leaking it', async () => {
    const fetchPage = vi
      .fn<(query: HistoryQuery) => Promise<Page<string>>>()
      .mockRejectedValue(new TypeError('network down'))

    const { result } = renderHook(() => useHistory(fetchPage, EMPTY_FILTERS, 'action'))

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.error).toBeInstanceOf(ApiError)
    expect(result.current.error?.status).toBe(500)
  })
})
