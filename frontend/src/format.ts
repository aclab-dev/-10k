/**
 * Formatea un timestamp ISO del backend (UTC) en la hora local del navegador.
 *
 * Los filtros de fecha también trabajan en hora local, así que la tabla y los
 * filtros hablan el mismo idioma. Devuelve el string crudo si no parsea.
 */
export function formatTimestamp(iso: string): string {
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  return parsed.toLocaleString('es-AR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}
