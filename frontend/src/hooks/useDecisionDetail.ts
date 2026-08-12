import { useEffect, useState } from 'react'
import { ApiError, getDecision, type DecisionOut } from '../api/client'

interface UseDecisionDetailResult {
  decision: DecisionOut | null
  loading: boolean
  error: ApiError | null
}

/**
 * Detalle de una decisión desde `GET /api/decisions/{id}`.
 *
 * Se relee del backend en vez de reusar la fila de la tabla: la fila puede venir
 * de una página cargada hace rato, y el drill-down es justo donde importa que el
 * dato esté fresco. `decisionId` null limpia el estado (panel cerrado).
 */
export function useDecisionDetail(decisionId: string | null): UseDecisionDetailResult {
  const [decision, setDecision] = useState<DecisionOut | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (decisionId === null) {
      setDecision(null)
      setError(null)
      setLoading(false)
      return
    }

    let cancelled = false
    setLoading(true)

    getDecision(decisionId)
      .then((data) => {
        if (cancelled) return
        setDecision(data)
        setError(null)
      })
      .catch((err) => {
        if (cancelled) return
        setDecision(null)
        setError(err instanceof ApiError ? err : new ApiError(500, 'Error inesperado'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [decisionId])

  return { decision, loading, error }
}
