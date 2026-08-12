import type { RiskValidationOut } from '../api/client'
import { formatTimestamp } from '../format'
import { DetailPanel, DetailRow } from './DetailPanel'

interface RiskValidationDetailProps {
  validation: RiskValidationOut
  onClose: () => void
}

/**
 * Drill-down de una validación del Risk Engine.
 *
 * Renderiza desde la fila del listado y no hace un fetch propio: el backend no
 * expone `GET /api/risk/validations/{id}`, y el listado ya devuelve el registro
 * completo (incluido `reasons`).
 */
export function RiskValidationDetail({ validation, onClose }: RiskValidationDetailProps) {
  return (
    <DetailPanel title="Detalle de la validación" onClose={onClose}>
      <DetailRow label="ID" value={<code>{validation.id}</code>} />
      <DetailRow label="Timestamp" value={formatTimestamp(validation.timestamp)} />
      <DetailRow label="Símbolo" value={validation.symbol} />
      <DetailRow label="Resultado" value={validation.result} />
      <DetailRow label="Margen original" value={validation.original_margin} />
      <DetailRow label="Leverage original" value={validation.original_leverage} />
      <DetailRow label="Margen ajustado" value={validation.adjusted_margin} />
      <DetailRow label="Leverage ajustado" value={validation.adjusted_leverage} />
      <DetailRow label="Pérdida diaria al chequear" value={validation.daily_loss_at_check} />
      <DetailRow label="Pérdida total al chequear" value={validation.total_loss_at_check} />
      <DetailRow label="Bot run" value={<code>{validation.bot_run_id}</code>} />
      <DetailRow
        label="Motivos"
        value={
          validation.reasons && (
            <pre className="detail-reasons">{JSON.stringify(validation.reasons, null, 2)}</pre>
          )
        }
      />
    </DetailPanel>
  )
}
