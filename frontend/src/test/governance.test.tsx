import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { CaseGovernance } from '../CaseGovernance'
import { Controls, ControlsError, type CaseView } from '../controls'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

const view: CaseView = {
  case_id: 'case-1', lifecycle: 'CLOSED', revision: 4, route_revision: 3,
  done: true, candidates: [], rendered_route_maps: [], search_progress: {},
  watch: [], unobserved_windows: [], errors: [],
}

function setup() {
  const client = new Controls('csrf')
  const decisions = vi.spyOn(client, 'decisions').mockResolvedValue([{
    decision_id: 'decision-1', candidate_id: 'candidate-1', tracklet_id: 'tracklet-1',
    decision: 'CONFIRM', active: true, occurred_at: '2026-01-01T00:00:00Z',
  }])
  const events = vi.spyOn(client, 'events').mockResolvedValue([{ event_type: 'CASE_CLOSED' }])
  const holds = vi.spyOn(client, 'legalHolds').mockResolvedValue([])
  const revoke = vi.spyOn(client, 'revoke')
  const props = { client, view, canReview: true, canLegalHold: true, onView: vi.fn(), refresh: vi.fn() }
  return { client, decisions, events, holds, revoke, props }
}

it('keeps successful sections and clears a read error after explicit reload succeeds', async () => {
  const { props, events, decisions, revoke } = setup()
  events.mockRejectedValueOnce(new ControlsError('Connection to Controls was interrupted. Please retry.', 503, 'CONTROLS_UNAVAILABLE', true))
  render(<CaseGovernance {...props}/>)
  expect(await screen.findByRole('alert')).toHaveTextContent('Case events: Connection to Controls was interrupted')
  expect(screen.getByText('Decision history (1)')).toBeInTheDocument()
  expect(screen.getByText('Legal holds (0 active)')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Reload history' }))
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(screen.getByText('Case events (1)')).toBeInTheDocument()
  expect(events).toHaveBeenCalledTimes(2)
  expect(decisions).toHaveBeenCalledTimes(2)
  expect(revoke).not.toHaveBeenCalled()
})

it('clears the old error on a successful revision-driven reload', async () => {
  const { props, events } = setup()
  events.mockRejectedValueOnce(new Error('Temporary interruption'))
  const { rerender } = render(<CaseGovernance {...props}/>)
  await screen.findByRole('alert')
  rerender(<CaseGovernance {...props} view={{ ...view, revision: 5 }}/>)
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(events).toHaveBeenCalledTimes(2)
})

it('workspace refresh reloads history even when the case revision has not changed', async () => {
  const { props, events } = setup()
  events.mockRejectedValueOnce(new Error('Temporary interruption'))
  const { rerender } = render(<CaseGovernance {...props} refreshKey={0}/>)
  await screen.findByRole('alert')
  rerender(<CaseGovernance {...props} refreshKey={1}/>)
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(events).toHaveBeenCalledTimes(2)
})

it('does not retry indefinitely or turn failed loads into a fake successful empty response', async () => {
  const { props, events } = setup()
  events.mockRejectedValue(new ControlsError('Controls did not respond in time. Please retry.', 504, 'CONTROLS_TIMEOUT', true))
  render(<CaseGovernance {...props}/>)
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: 'Reload history' }))
  await waitFor(() => expect(events).toHaveBeenCalledTimes(2))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Reload history' })).toBeEnabled())
  expect(screen.getByRole('alert')).toHaveTextContent('Case events: Controls did not respond in time')
})

it('does not erase an unsuccessful operator action when read requests recover', async () => {
  const { props, revoke } = setup()
  revoke.mockRejectedValue(new ControlsError('Decision could not be revoked.', 409))
  const { rerender } = render(<CaseGovernance {...props}/>)
  const reason = await screen.findByRole('textbox', { name: 'Revoke reason for decision-1' })
  fireEvent.change(reason, { target: { value: 'Review correction' } })
  fireEvent.submit(reason.closest('form')!)
  expect(await screen.findByRole('alert')).toHaveTextContent('Decision could not be revoked.')
  rerender(<CaseGovernance {...props} refreshKey={1}/>)
  await waitFor(() => expect(props.client.events).toHaveBeenCalledTimes(2))
  expect(screen.getByRole('alert')).toHaveTextContent('Decision could not be revoked.')
  expect(revoke).toHaveBeenCalledOnce()
})

it('aborts reads for an unmounted case without showing stale errors', async () => {
  const { props, events } = setup()
  let fail: (error: Error) => void = () => {}
  events.mockReturnValue(new Promise((_, reject) => { fail = reject }))
  const { unmount } = render(<CaseGovernance {...props}/>)
  const signal = events.mock.calls[0][1]
  unmount()
  expect(signal?.aborted).toBe(true)
  fail(new Error('Aborted old case request'))
  await Promise.resolve()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
