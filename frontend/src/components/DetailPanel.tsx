import type { ReactNode } from 'react'

interface DetailPanelProps {
  title: string
  onClose: () => void
  children: ReactNode
}

/** Contenedor del drill-down: encabezado, botón de cierre y cuerpo. */
export function DetailPanel({ title, onClose, children }: DetailPanelProps) {
  return (
    <aside className="detail-panel" aria-label={title}>
      <header className="detail-header">
        <h2>{title}</h2>
        <button type="button" onClick={onClose}>
          Cerrar
        </button>
      </header>
      <dl className="detail-body">{children}</dl>
    </aside>
  )
}

interface DetailRowProps {
  label: string
  value: ReactNode
}

export function DetailRow({ label, value }: DetailRowProps) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value === null || value === undefined || value === '' ? '—' : value}</dd>
    </>
  )
}
