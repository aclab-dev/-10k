import { formatTimestamp } from '../format'
import { useDecisionDetail } from '../hooks/useDecisionDetail'
import { DetailPanel, DetailRow } from './DetailPanel'

interface DecisionDetailProps {
  decisionId: string
  onClose: () => void
}

export function DecisionDetail({ decisionId, onClose }: DecisionDetailProps) {
  const { decision, loading, error } = useDecisionDetail(decisionId)

  if (loading) {
    return (
      <DetailPanel title="Detalle de la decisión" onClose={onClose}>
        <DetailRow label="Estado" value="Cargando…" />
      </DetailPanel>
    )
  }

  if (error || decision === null) {
    return (
      <DetailPanel title="Detalle de la decisión" onClose={onClose}>
        <DetailRow
          label="Error"
          value={<span className="error-message">{error?.message ?? 'Decisión no encontrada'}</span>}
        />
      </DetailPanel>
    )
  }

  return (
    <DetailPanel title="Detalle de la decisión" onClose={onClose}>
      <DetailRow label="ID" value={<code>{decision.id}</code>} />
      <DetailRow label="Timestamp" value={formatTimestamp(decision.timestamp)} />
      <DetailRow label="Símbolo" value={decision.symbol} />
      <DetailRow label="Acción" value={decision.action} />
      <DetailRow label="Dirección" value={decision.direction} />
      <DetailRow
        label="Confianza"
        value={decision.confidence === null ? null : decision.confidence.toFixed(2)}
      />
      <DetailRow label="Margen (USDT)" value={decision.margin_usdt} />
      <DetailRow label="Leverage" value={decision.leverage} />
      <DetailRow label="Stop loss" value={decision.stop_loss} />
      <DetailRow label="Take profit" value={decision.take_profit} />
      <DetailRow label="Bot run" value={<code>{decision.bot_run_id}</code>} />
      <DetailRow
        label="Razonamiento"
        value={decision.reasoning && <p className="detail-reasoning">{decision.reasoning}</p>}
      />
    </DetailPanel>
  )
}
