import { useEffect, useRef, useState } from 'react'
import { ApiError, triggerKillSwitch } from '../api/client'

const DISABLED_STATES = new Set(['KILL_SWITCH_TRIGGERED', 'MANUAL_PAUSED'])

interface KillSwitchButtonProps {
  currentState: string | null
  onTriggered: () => void
}

export function KillSwitchButton({ currentState, onTriggered }: KillSwitchButtonProps) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const disabled = currentState !== null && DISABLED_STATES.has(currentState)

  // <dialog>.showModal() da focus trap y cierre por Escape gratis. El
  // listener de 'cancel' (que Escape dispara) bloquea el cierre mientras
  // hay un submit en curso, igual que closeModal() hacía antes.
  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const handleCancel = (e: Event) => {
      if (submitting) e.preventDefault()
    }
    dialog.addEventListener('cancel', handleCancel)
    return () => dialog.removeEventListener('cancel', handleCancel)
  }, [submitting])

  function openModal() {
    setReason('')
    setError(null)
    dialogRef.current?.showModal()
  }

  function closeModal() {
    if (submitting) return
    dialogRef.current?.close()
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
      dialogRef.current?.close()
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

      <dialog ref={dialogRef} className="modal" aria-labelledby="kill-switch-title">
        <h2 id="kill-switch-title">Detener el bot manualmente</h2>
        <p>
          Esta acción transiciona el bot a <strong>KILL_SWITCH_TRIGGERED</strong> y requiere
          revisión manual para retomar. El worker corre en otro proceso y no se detiene al
          instante: si ya está operando un símbolo, ese símbolo puede terminar de procesarse
          (incluida la apertura de una orden) antes de frenar. Indicá el motivo:
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
      </dialog>
    </>
  )
}
