import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { Workspace } from '../Workspace'
import { SearchStages } from '../SearchStages'
import { Controls, type CaseView } from '../controls'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

function closedCase(overrides: Partial<CaseView> = {}): CaseView {
  return {
    case_id: 'closed', revision: 4, route_revision: 3, lifecycle: 'CLOSED',
    done: true, candidates: [], rendered_route_maps: [], search_progress: {},
    errors: [], unobserved_windows: [], watch: [],
    clips: [{ clip_id: 'clip-1', state: 'CONFIRMED', status: 'READY', url: '/v1/cases/closed/clips/clip-1' }],
    ...overrides,
  }
}

function mediaClient() {
  const BrowserURL = URL
  const create = vi.fn(() => 'blob:merged-video')
  const revoke = vi.fn()
  vi.stubGlobal('URL', class extends BrowserURL {
    static createObjectURL = create
    static revokeObjectURL = revoke
  })
  const client = new Controls()
  const media = vi.spyOn(client, 'media').mockResolvedValue(new Blob(['video'], { type: 'video/mp4' }))
  return { client, media, create, revoke }
}

it('opens a closed case on its route and explains missing accepted evidence', () => {
  const view: CaseView = {
    case_id: 'closed', revision: 4, route_revision: 3, lifecycle: 'CLOSED',
    done: true, candidates: [], rendered_route_maps: [], search_progress: {},
    errors: [], unobserved_windows: [], watch: [],
    route_presentation: { position_state: 'UNKNOWN' },
  }
  render(<Workspace view={view} cycle={0}/>)
  expect(screen.getByRole('button', { name: 'Route map' })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.getByRole('status')).toHaveTextContent('A floor plan alone does not indicate a tracked route')
  fireEvent.click(screen.getByRole('button', { name: 'Merged video' }))
  expect(screen.getByText('Merged video has not been generated')).toBeInTheDocument()
  expect(screen.queryByText('No camera frame available')).not.toBeInTheDocument()
})

it('replaces live preview with recorded media when the active search closes without auto-export', () => {
  const requestExport = vi.fn()
  const { rerender } = render(<Workspace view={closedCase({ lifecycle: 'SEARCHING', done: false })}
    cycle={0} requestExport={requestExport}/>)
  expect(screen.getByRole('button', { name: 'Live preview' })).toHaveAttribute('aria-pressed', 'true')
  rerender(<Workspace view={closedCase()} cycle={1} requestExport={requestExport}/>)
  expect(screen.queryByText('No camera frame available')).not.toBeInTheDocument()
  expect(screen.queryByText(/published feeds/)).not.toBeInTheDocument()
  expect(requestExport).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Generate merged video' }))
  expect(requestExport).toHaveBeenCalledOnce()
  expect(screen.getByText('Export finalizes the case and ends candidate review.')).toBeInTheDocument()
})

it('plays ready merged MP4 inline, without restarting it on each case poll', async () => {
  const { client, media, revoke } = mediaClient()
  const view = closedCase({ lifecycle: 'SAVED', export: {
    export_request_id: 'export-1', status: 'READY', url: '/v1/cases/closed/export',
  } })
  const { rerender, unmount } = render(<Workspace view={view} client={client} cycle={0}/>)
  fireEvent.click(screen.getByRole('button', { name: 'Merged video' }))
  const video = await screen.findByLabelText('Merged case video')
  expect(video).toHaveAttribute('src', 'blob:merged-video')
  expect(video).toHaveAttribute('controls')
  expect(screen.getByRole('link', { name: 'Download MP4' })).toHaveAttribute('download', 'closed.mp4')
  expect(media).toHaveBeenCalledWith('/v1/cases/closed/export', 'video/mp4', expect.any(AbortSignal))
  rerender(<Workspace view={{ ...view, revision: 5 }} client={client} cycle={1}/>)
  expect(media).toHaveBeenCalledOnce()
  expect(screen.getByLabelText('Merged case video')).toBe(video)
  rerender(<Workspace view={{ ...view, export: { ...view.export, export_request_id: 'export-2' } }} client={client} cycle={2}/>)
  await waitFor(() => expect(media).toHaveBeenCalledTimes(2))
  unmount()
  expect(revoke).toHaveBeenCalledTimes(2)
})

it('shows export processing instead of playing an older ready export', () => {
  const { client, media } = mediaClient()
  render(<Workspace view={closedCase({ lifecycle: 'EXPORTING', export: {
    export_request_id: 'old-export', status: 'READY', url: '/v1/cases/closed/export',
  } })} client={client} cycle={0}/>)
  fireEvent.click(screen.getByRole('button', { name: 'Merged video' }))
  expect(screen.getByRole('status')).toHaveTextContent('Clip worker is assembling')
  expect(screen.queryByLabelText('Merged case video')).not.toBeInTheDocument()
  expect(media).not.toHaveBeenCalled()
  expect(screen.queryByRole('button', { name: 'Generate merged video' })).not.toBeInTheDocument()
})

it('reports a failed export and offers only an explicit authorized retry', () => {
  const requestExport = vi.fn()
  render(<Workspace view={closedCase({ export: { status: 'FAILED', error_code: 'MEDIA_FAILED' } })}
    cycle={0} requestExport={requestExport}/>)
  fireEvent.click(screen.getByRole('button', { name: 'Merged video' }))
  expect(screen.getByRole('alert')).toHaveTextContent('Media failed')
  expect(requestExport).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry merged video' }))
  expect(requestExport).toHaveBeenCalledOnce()
})

it('does not offer export to a read-only user or enable it for unready clips', () => {
  const view = closedCase({ clips: [] })
  const { rerender } = render(<Workspace view={view} cycle={0}/>)
  fireEvent.click(screen.getByRole('button', { name: 'Merged video' }))
  expect(screen.queryByRole('button', { name: 'Generate merged video' })).not.toBeInTheDocument()
  rerender(<Workspace view={view} cycle={1} requestExport={vi.fn()}/>)
  expect(screen.getByRole('button', { name: 'Generate merged video' })).toBeDisabled()
  rerender(<Workspace view={closedCase({ clips: [{ clip_id: 'clip-1', state: 'CONFIRMED', status: 'PENDING' }] })}
    cycle={2} requestExport={vi.fn()}/>)
  expect(screen.getByRole('button', { name: 'Generate merged video' })).toBeDisabled()
  expect(screen.getByText('Waiting for all confirmed clips to be ready.')).toBeInTheDocument()
})

it('reports merged media loading failures without showing a fake playable video', async () => {
  const { client, media } = mediaClient()
  media.mockRejectedValue(new Error('Video unavailable.'))
  render(<Workspace view={closedCase({ export: { status: 'READY', url: '/v1/cases/closed/export' } })}
    client={client} cycle={0}/>)
  fireEvent.click(screen.getByRole('button', { name: 'Merged video' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Video unavailable.')
  expect(screen.queryByLabelText('Merged case video')).not.toBeInTheDocument()
})

it('does not present blocked Routing as active video search', () => {
  render(<SearchStages lifecycle="SEARCHING" mode="CATCHING_UP" done={false} processingStatus="BLOCKED"/>)
  expect(screen.getByRole('alert')).toHaveTextContent('Search cannot advance')
  expect(document.querySelector('[aria-current="step"]')).toBeNull()
})

it('fits the entire map by default and resets both zoom dimensions on reset or floor switch', async () => {
  const { client } = mediaClient()
  render(<Workspace view={closedCase({ rendered_route_maps: [
    { floor_id: 'warehouse', url: '/v1/cases/closed/routes/3/warehouse.svg', alt_text: 'Warehouse route' },
    { floor_id: 'upstairs', url: '/v1/cases/closed/routes/3/upstairs.svg', alt_text: 'Upstairs route' },
  ] })} client={client} cycle={0}/>)
  const map = await screen.findByAltText('Warehouse route')
  expect(map.parentElement).toHaveStyle({ width: '100%', height: '100%' })
  expect(screen.getByRole('button', { name: 'Zoom out' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
  expect(map.parentElement).toHaveStyle({ width: '125%', height: '125%' })
  fireEvent.click(screen.getByRole('button', { name: 'Reset zoom' }))
  expect(map.parentElement).toHaveStyle({ width: '100%', height: '100%' })
  fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
  fireEvent.change(screen.getByRole('combobox', { name: 'Floor' }), { target: { value: 'upstairs' } })
  const upstairs = await screen.findByAltText('Upstairs route')
  expect(upstairs.parentElement).toHaveStyle({ width: '100%', height: '100%' })
})
