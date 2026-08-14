import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { KillSwitchButton } from './KillSwitchButton'
import { ApiError, triggerKillSwitch } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, triggerKillSwitch: vi.fn() }
})

const mockedTriggerKillSwitch = vi.mocked(triggerKillSwitch)

describe('KillSwitchButton', () => {
  afterEach(() => {
    mockedTriggerKillSwitch.mockReset()
  })

  it('requires a non-empty reason before submitting', async () => {
    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    await user.click(screen.getByRole('button', { name: 'Sí, detener el bot' }))

    expect(await screen.findByText('El motivo es obligatorio')).toBeInTheDocument()
    expect(mockedTriggerKillSwitch).not.toHaveBeenCalled()
  })

  it('disables the confirm button while submitting and calls onTriggered on success', async () => {
    const onTriggered = vi.fn()
    let resolveRequest: (() => void) | undefined
    mockedTriggerKillSwitch.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRequest = () =>
            resolve({
              bot_run_id: 'run-1',
              state: 'KILL_SWITCH_TRIGGERED',
              previous_state: 'ACTIVE',
              reason: 'motivo de prueba',
              triggered_at: new Date().toISOString(),
            })
        }),
    )

    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={onTriggered} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    await user.type(screen.getByPlaceholderText('Motivo del kill switch…'), 'motivo de prueba')
    await user.click(screen.getByRole('button', { name: 'Sí, detener el bot' }))

    expect(await screen.findByRole('button', { name: 'Deteniendo…' })).toBeDisabled()

    resolveRequest?.()

    await waitFor(() => expect(onTriggered).toHaveBeenCalledTimes(1))
  })

  it('shows the API error message when the request fails', async () => {
    mockedTriggerKillSwitch.mockRejectedValue(new ApiError(409, 'Conflicto de estado'))

    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    await user.type(screen.getByPlaceholderText('Motivo del kill switch…'), 'motivo')
    await user.click(screen.getByRole('button', { name: 'Sí, detener el bot' }))

    expect(await screen.findByText('Conflicto de estado')).toBeInTheDocument()
  })

  it('disables the button when the current state cannot accept the kill switch', () => {
    render(<KillSwitchButton currentState="MANUAL_PAUSED" onTriggered={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Kill Switch' })).toBeDisabled()
  })

  it('enables the button when the current state can accept the kill switch', () => {
    render(<KillSwitchButton currentState="HALTED" onTriggered={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Kill Switch' })).toBeEnabled()
  })

  it('enables the button when currentState is null (status still loading)', () => {
    render(<KillSwitchButton currentState={null} onTriggered={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Kill Switch' })).toBeEnabled()
  })

  it('trims the reason before sending it', async () => {
    mockedTriggerKillSwitch.mockResolvedValue({
      bot_run_id: 'run-1',
      state: 'KILL_SWITCH_TRIGGERED',
      previous_state: 'ACTIVE',
      reason: 'motivo',
      triggered_at: new Date().toISOString(),
    })

    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    await user.type(screen.getByPlaceholderText('Motivo del kill switch…'), '  motivo  ')
    await user.click(screen.getByRole('button', { name: 'Sí, detener el bot' }))

    await waitFor(() => expect(mockedTriggerKillSwitch).toHaveBeenCalledWith('motivo'))
  })

  it('rejects a whitespace-only reason', async () => {
    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    await user.type(screen.getByPlaceholderText('Motivo del kill switch…'), '   ')
    await user.click(screen.getByRole('button', { name: 'Sí, detener el bot' }))

    expect(await screen.findByText('El motivo es obligatorio')).toBeInTheDocument()
    expect(mockedTriggerKillSwitch).not.toHaveBeenCalled()
  })

  it('closes the modal without calling the API when Cancelar is clicked', async () => {
    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    const dialog = document.querySelector('dialog') as HTMLDialogElement
    expect(dialog).toHaveAttribute('open')

    await user.click(screen.getByRole('button', { name: 'Cancelar' }))

    expect(dialog).not.toHaveAttribute('open')
    expect(mockedTriggerKillSwitch).not.toHaveBeenCalled()
  })

  it('resets the reason field when the modal is reopened', async () => {
    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    await user.type(screen.getByPlaceholderText('Motivo del kill switch…'), 'primer intento')
    await user.click(screen.getByRole('button', { name: 'Cancelar' }))

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))

    expect(screen.getByPlaceholderText('Motivo del kill switch…')).toHaveValue('')
  })

  it('ignores Cancelar while a submit is in flight', async () => {
    let resolveRequest: (() => void) | undefined
    mockedTriggerKillSwitch.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveRequest = () =>
            resolve({
              bot_run_id: 'run-1',
              state: 'KILL_SWITCH_TRIGGERED',
              previous_state: 'ACTIVE',
              reason: 'motivo',
              triggered_at: new Date().toISOString(),
            })
        }),
    )

    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    await user.type(screen.getByPlaceholderText('Motivo del kill switch…'), 'motivo')
    await user.click(screen.getByRole('button', { name: 'Sí, detener el bot' }))

    const dialog = document.querySelector('dialog') as HTMLDialogElement
    await user.click(screen.getByRole('button', { name: 'Cancelar' }))
    expect(dialog).toHaveAttribute('open')

    resolveRequest?.()
    await waitFor(() => expect(dialog).not.toHaveAttribute('open'))
  })

  it('prevents the native cancel (Escape) while a submit is in flight', async () => {
    mockedTriggerKillSwitch.mockImplementation(() => new Promise(() => {}))

    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))
    await user.type(screen.getByPlaceholderText('Motivo del kill switch…'), 'motivo')
    await user.click(screen.getByRole('button', { name: 'Sí, detener el bot' }))

    const dialog = document.querySelector('dialog') as HTMLDialogElement
    const cancelEvent = new Event('cancel', { cancelable: true })
    dialog.dispatchEvent(cancelEvent)

    expect(cancelEvent.defaultPrevented).toBe(true)
  })

  it('allows the native cancel (Escape) when not submitting', async () => {
    const user = userEvent.setup()
    render(<KillSwitchButton currentState="ACTIVE" onTriggered={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: 'Kill Switch' }))

    const dialog = document.querySelector('dialog') as HTMLDialogElement
    const cancelEvent = new Event('cancel', { cancelable: true })
    dialog.dispatchEvent(cancelEvent)

    expect(cancelEvent.defaultPrevented).toBe(false)
  })
})
