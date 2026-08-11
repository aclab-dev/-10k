import { useEffect, useState } from 'react'
import { ApiError, type HistoryQuery, type Page } from '../api/client'

export const PAGE_SIZE = 25

/** Estado de los filtros tal como los edita el usuario (valores de los inputs). */
export interface HistoryFiltersState {
  /** Valor de un `<input type="datetime-local">`: hora local, sin offset. */
  from: string
  to: string
  /** Símbolo, o '' para todos. */
  symbol: string
  /** Acción (decisiones) o resultado (bloqueos), o '' para todos. */
  kind: string
}

export const EMPTY_FILTERS: HistoryFiltersState = { from: '', to: '', symbol: '', kind: '' }

/**
 * Convierte el valor de un `datetime-local` (hora local del navegador) a ISO 8601 UTC.
 *
 * El backend interpreta un timestamp sin offset como UTC, así que mandar el valor
 * crudo del input haría que el filtro signifique otra hora para cualquiera que no
 * esté en UTC. Devuelve undefined si está vacío o no parsea.
 */
export function toIsoUtc(localValue: string): string | undefined {
  if (localValue === '') return undefined
  const parsed = new Date(localValue)
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString()
}

/**
 * Arma el query del backend. `kindParam` nombra el filtro que cambia según la
 * pestaña: `action` para decisiones, `result` para bloqueos.
 */
export function toHistoryQuery(
  filters: HistoryFiltersState,
  offset: number,
  kindParam: 'action' | 'result',
): HistoryQuery & Record<string, unknown> {
  return {
    symbol: filters.symbol || undefined,
    from_ts: toIsoUtc(filters.from),
    to_ts: toIsoUtc(filters.to),
    [kindParam]: filters.kind || undefined,
    limit: PAGE_SIZE,
    offset,
  }
}

interface UseHistoryResult<T> {
  items: T[]
  total: number
  offset: number
  loading: boolean
  error: ApiError | null
  setOffset: (offset: number) => void
}

/**
 * Listado paginado con filtros. `fetchPage` debe ser una referencia estable
 * (función de módulo o memoizada): cambiarla dispara una nueva carga.
 *
 * `filters` se compara por identidad, así que también tiene que ser estable
 * (típicamente el valor de un useState): un objeto nuevo en cada render
 * dispararía una carga por render. Cambiarlo resetea el offset a 0 — si no,
 * filtrar desde la página 3 dejaría al usuario mirando una página vacía de un
 * resultado más chico.
 */
export function useHistory<T, Q extends HistoryQuery>(
  fetchPage: (query: Q) => Promise<Page<T>>,
  filters: HistoryFiltersState,
  kindParam: 'action' | 'result',
): UseHistoryResult<T> {
  const [page, setPage] = useState<Page<T> | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(true)
  const [offset, setOffset] = useState(0)
  const [appliedFilters, setAppliedFilters] = useState(filters)

  // Ajuste de estado durante el render (patrón recomendado por React para estado
  // derivado): evita el fetch extra que provocaría hacerlo en un useEffect.
  if (filters !== appliedFilters) {
    setAppliedFilters(filters)
    setOffset(0)
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)

    // El cast es necesario porque la clave del filtro por pestaña se arma en
    // runtime (`action` o `result`) y TS no puede probar que coincide con Q.
    fetchPage(toHistoryQuery(filters, offset, kindParam) as Q)
      .then((data) => {
        if (cancelled) return
        setPage(data)
        setError(null)
      })
      .catch((err) => {
        if (cancelled) return
        // Se descarta la página anterior: dejarla visible junto al mensaje de
        // error mostraría filas que ya no corresponden a los filtros activos.
        setPage(null)
        setError(err instanceof ApiError ? err : new ApiError(500, 'Error inesperado'))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [fetchPage, kindParam, filters, offset])

  return {
    items: page?.items ?? [],
    total: page?.total ?? 0,
    offset,
    loading,
    error,
    setOffset,
  }
}
