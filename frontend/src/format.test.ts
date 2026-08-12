import { describe, expect, it } from 'vitest'
import { formatTimestamp } from './format'

describe('formatTimestamp', () => {
  it('renders a UTC timestamp in the local timezone of the browser', () => {
    const iso = '2026-03-02T12:00:00Z'

    // El offset del runner es desconocido: se compara contra el mismo instante
    // formateado por Date, no contra un string fijo.
    expect(formatTimestamp(iso)).toBe(
      new Date(iso).toLocaleString('es-AR', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      }),
    )
  })

  it('returns the raw value when it does not parse', () => {
    expect(formatTimestamp('no-es-una-fecha')).toBe('no-es-una-fecha')
  })
})
