import { useCallback, useEffect, useState } from 'react'
import { ApiError, getStatus, type BotStatusOut } from '../api/client'

const POLL_INTERVAL_MS = 5000

interface UseStatusResult {
  status: BotStatusOut | null
  error: ApiError | null
  loading: boolean
  refresh: () => void
}

export function useStatus(): UseStatusResult {
  const [status, setStatus] = useState<BotStatusOut | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshTick, setRefreshTick] = useState(0)

  const refresh = useCallback(() => setRefreshTick((tick) => tick + 1), [])

  useEffect(() => {
    let cancelled = false

    async function fetchStatus() {
      try {
        const data = await getStatus()
        if (!cancelled) {
          setStatus(data)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err : new ApiError(500, 'Error inesperado'))
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    fetchStatus()
    const interval = setInterval(fetchStatus, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [refreshTick])

  return { status, error, loading, refresh }
}
