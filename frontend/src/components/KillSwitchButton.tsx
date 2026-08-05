import { useState } from 'react'
import { ApiError, triggerKillSwitch } from '../api/client'

const DISABLED_STATES = new Set(['KILL_SWITCH_TRIGGERED', 'HALTED'])

interface KillSwitchButtonProps {
  currentState: string | null
  onTriggered: () => void
}

export function KillSwitchButton({ currentState, onTriggered }: KillSwitchButtonProps) {
  const [modalOpen, setModalOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const disabled = currentState !== null && DISABLED_STATES.has(currentState)

  function openModal() {
    setReason('')
    setError(null)
    setModalOpen(true)
  }

  function closeModal() {
    if (submitting) return
    setModalOpen(false)
  }

  async function confirmKillSwitch() {
    if (!reason.trim()) {
      setError('El motivo es obligatorio')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await triggerKillSwitch(reason.trim())
      setModalOpen(false)
      onTriggered()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Error inesperado')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <>
      <button
        type="button"
        className="kill-switch-button"
        onClick={openModal}
        disabled={disabled}
      >
        Kill Switch
      </button>

      {modalOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true">
          <div className="modal">
            <h2>Detener el bot manualmente</h2>
            <p>
              Esta acción transiciona el bot a <strong>KILL_SWITCH_TRIGGERED</strong> y requiere
              revisión manual para retomar. Indicá el motivo:
            </p>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Motivo del kill switch…"
              rows={3}
              disabled={submitting}
              autoFocus
            />
            {error && <p className="error-message">{error}</p>}
            <div className="modal-actions">
              <button type="button" onClick={closeModal} disabled={submitting}>
                Cancelar
              </button>
              <button
                type="button"
                className="danger"
                onClick={confirmKillSwitch}
                disabled={submitting}
              >
                {submitting ? 'Deteniendo…' : 'Sí, detener el bot'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}
