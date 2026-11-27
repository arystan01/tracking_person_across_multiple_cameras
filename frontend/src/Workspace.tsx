import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Activity, ArrowUpRight, Camera, Check, ChevronRight, Crosshair, Eye, Film, Layers, Map, Minus, Plus, RotateCcw, VideoOff, X } from 'lucide-react'
import { casePath, type Candidate, type Clip, type CaseView, type Controls, type Decision } from './controls'

export const label = (value?: string | null) => value
  ? value.replaceAll('_', ' ').toLowerCase().replace(/^./, (letter) => letter.toUpperCase())
  : 'Unavailable'

const clock = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'Asia/Seoul' })
export function time(value?: string) {
  const parsed = value ? Date.parse(value) : NaN
  return Number.isFinite(parsed) ? clock.format(parsed) : '—'
}
export const pendingReview = (candidate: Candidate) => candidate.decision_state === 'UNREVIEWED'

export function Modal({ title, close, children, className }: { title: string; close: () => void; children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])
  return (
    <dialog ref={ref} className={className} onCancel={(event) => { event.preventDefault(); close() }} aria-labelledby="dialog-title">
      <div className="dialog-head"><h2 id="dialog-title">{title}</h2><button className="icon-button" aria-label="Close dialog" onClick={close}><X size={19}/></button></div>
      {children}
    </dialog>
  )
}

function useMedia(client: Controls | undefined, path: string | undefined, mime: string, cycle?: number, identity = path) {
  const [image, setImage] = useState<{ identity: string; src: string }>()
  const [failure, setFailure] = useState<{ identity: string; error: string }>()
  const held = useRef<string | undefined>(undefined)
  useEffect(() => () => {
    if (held.current) URL.revokeObjectURL(held.current)
    held.current = undefined
  }, [])
  useEffect(() => {
    if (!client || !path || !identity) return
    const abort = new AbortController()
    client.media(path, mime, abort.signal).then((blob) => {
      if (abort.signal.aborted) return
      const src = URL.createObjectURL(blob)
      if (held.current) URL.revokeObjectURL(held.current)
      held.current = src
      setImage({ identity, src })
      setFailure(undefined)
    }).catch((error: unknown) => {
      if (abort.signal.aborted) return
      setFailure({ identity, error: error instanceof Error ? error.message : 'Media unavailable.' })
    })
    return () => abort.abort()
  }, [client, path, mime, cycle, identity])
  return { src: image?.identity === identity ? image?.src : undefined, error: failure?.identity === identity ? failure?.error : undefined }
}

function CameraPanel({ view, client, cycle }: { view: CaseView; client?: Controls; cycle: number }) {
  const [now, setNow] = useState(0)
  useEffect(() => {
    setNow(Date.now())
    const timer = window.setInterval(() => setNow(Date.now()), 5_000)
    return () => window.clearInterval(timer)
  }, [])
  const expiresAt = view.live?.expires_at ? Date.parse(view.live.expires_at) : NaN
  const observedAt = view.live?.observed_at ? Date.parse(view.live.observed_at) : NaN
  const expired = now > 0 && Number.isFinite(expiresAt) && expiresAt <= now
  const stale = now > 0 && !expired && Number.isFinite(observedAt) && now - observedAt > 15_000
  const livePath = view.live?.stream_url && !expired ? casePath(view.case_id) + '/live' : undefined
  const live = useMedia(client, livePath, 'image/jpeg', cycle, view.live?.camera_id)
  const [expanded, setExpanded] = useState<string | null>(null)
  const watched = [...new Set(view.watch.map((item) => item.camera_id))]
    .filter((id) => id !== view.live?.camera_id)
  return (
    <>
      <div className="live-area">
        {live.src ? <button className="camera-tile live-tile" onClick={() => setExpanded(view.live!.camera_id)} aria-label="Expand live camera frame">
          <img src={live.src} alt={'Latest snapshot from ' + view.live?.camera_id}/>
          <span className="camera-shade"/>
          <span className="feed-flag"><i className={'dot ' + (!live.error && !stale ? 'green' : '')}/>{live.error ? 'Unavailable' : stale ? 'Stale frame' : 'Fresh frame'}</span>
          <span className="camera-caption"><span><small>{time(view.live?.observed_at)} KST</small><strong>{view.live?.camera_id}</strong></span><ArrowUpRight size={17}/></span>
        </button> : <div className="media-empty"><VideoOff size={28}/><strong>No camera frame available</strong>
          <span>{view.lifecycle === 'CLOSED' || view.lifecycle === 'SAVED'
            ? 'Search is closed. Use Route map and Case recordings to review the recorded evidence.'
            : expired ? 'The last published frame expired.' : live.error ?? 'A frame will appear when Controls publishes one for this case.'}</span>
        </div>}
        {watched.length > 0 && <div className="observed-cameras" aria-label="Cameras on watch">
          <strong>Cameras on watch</strong>
          {watched.map((id) => <span key={id}><Camera size={15}/>{id}</span>)}
        </div>}
      </div>
      {expanded && <Modal title={expanded} close={() => setExpanded(null)}>
        {live.src && view.live?.camera_id === expanded ? <img className="large-frame" src={live.src} alt={'Latest snapshot from ' + expanded}/>
        : <p className="muted">The frame is no longer available.</p>}
      </Modal>}
    </>
  )
}

function RoutePanel({ view, client }: { view: CaseView; client?: Controls }) {
  const [floor, setFloor] = useState<string>()
  const [zoom, setZoom] = useState(1)
  const map = view.rendered_route_maps.find((item) => item.floor_id === floor) ?? view.rendered_route_maps[0]
  const { src, error } = useMedia(client, map?.url, 'image/svg+xml')
  return <div className="map-shell">
    {view.route_presentation?.position_state === 'UNKNOWN' && <p className="notice" role="status">No accepted position is available for this route revision. A floor plan alone does not indicate a tracked route. Check candidate decisions and reported Routing errors.</p>}
    <div className="map-controls">
      {map ? <label><Layers size={16}/><select aria-label="Floor" value={map.floor_id} onChange={(event) => { setFloor(event.target.value); setZoom(1) }}>
        {view.rendered_route_maps.map((item) => <option key={item.floor_id}>{item.floor_id}</option>)}
      </select></label> : null}
      {src && <div className="zoom">
        <button className="icon-button" aria-label="Zoom out" disabled={zoom <= 1} onClick={() => setZoom((n) => Math.max(1, n - .25))}><Minus size={16}/></button>
        <button className="icon-button" aria-label="Reset zoom" title="Fit entire map to window" disabled={zoom === 1} onClick={() => setZoom(1)}><RotateCcw size={15}/></button>
        <button className="icon-button" aria-label="Zoom in" disabled={zoom >= 3} onClick={() => setZoom((n) => Math.min(3, n + .25))}><Plus size={16}/></button>
      </div>}
    </div>
    <div className="map-viewport" tabIndex={src ? 0 : undefined} aria-label="Route map viewport">
      {src ? <div className="map-image" style={{ width: zoom * 100 + '%', height: zoom * 100 + '%' }}>
        <img src={src} alt={map?.alt_text || 'Case route map'}/>
      </div> : <div className="media-empty"><Map size={28}/><strong>{error || (map ? 'Loading route map…' : 'Route map unavailable')}</strong></div>}
    </div>
    <RouteSightings view={view}/>
  </div>
}

function RouteSightings({ view }: { view: CaseView }) {
  const visits = view.best_hypothesis?.visits ?? []
  const lastObserved = view.estimate?.last_observed_camera
  const estimateTime = view.route_presentation?.search_area_as_of
  const hasSearchArea = view.route_presentation?.shown_layers?.includes('SEARCH_AREA')
  const alternatives = (view.alternatives ?? []).filter((route) => route.visits.length)
  if (!visits.length && !lastObserved && !hasSearchArea && !alternatives.length) return null

  return <div className="route-sightings">
    {lastObserved && <p>Last observed at <strong>{lastObserved}</strong>
      {view.estimate?.last_observed_time && ' · ' + time(view.estimate.last_observed_time) + ' KST'}</p>}
    {hasSearchArea && <p>Estimated search area shown on the map{estimateTime ? ' · ' + time(estimateTime) + ' KST' : ''}.
      {view.estimate?.valid_until && ' Valid until ' + time(view.estimate.valid_until) + ' KST.'}</p>}
    {visits.length > 0 && <details>
      <summary>Route sightings ({visits.length})</summary>
      <ol>{visits.map((visit, index) => <li key={visit.visit_id ?? visit.id ?? index}>
        {visit.camera_id ?? visit.camera ?? 'Camera unavailable'} · {time(visit.t_in)} KST
        {visit.source === 'LAST_KNOWN' && ' · Reported last known'}
      </li>)}</ol>
    </details>}
    {alternatives.length > 0 && <details>
      <summary>Alternative routes ({alternatives.length})</summary>
      <ol>{alternatives.map((route, index) => <li key={route.id ?? index}>
        {route.visits.map((visit) => visit.camera_id ?? visit.camera ?? 'Unknown camera').join(' → ')}
      </li>)}</ol>
    </details>}
  </div>
}

function MergedVideoPanel({ view, client, requestExport, busy }: {
  view: CaseView; client?: Controls; requestExport?: () => void; busy?: boolean
}) {
  const merged = view.export
  const preparing = view.lifecycle === 'EXPORTING' || ['PENDING', 'PROCESSING'].includes(merged?.status ?? '')
  const failed = ['FAILED', 'UNAVAILABLE'].includes(merged?.status ?? '')
  const confirmed = (view.clips ?? []).filter((clip) => clip.state === 'CONFIRMED')
  const clipsReady = confirmed.length > 0 && confirmed.every((clip) => clip.status === 'READY' && clip.url)
  const ready = !preparing && merged?.status === 'READY' && merged.url
  return <div className="merged-video-panel">
    <p className="muted">Confirmed sighting clips in route order, with short context before and after each sighting. Not full camera recordings.</p>
    {ready && client ? <VideoPlayback key={merged.export_request_id ?? merged.url} client={client}
      path={merged.url!} title="Merged case video" download={view.case_id + '.mp4'}/>
    : <div className="media-empty">
      <Film size={28}/>
      {preparing ? <><strong>Preparing merged video</strong><span role="status">Clip worker is assembling the confirmed clips. This panel updates automatically.</span></>
      : <>
        <strong>{failed ? 'Merged video could not be generated' : 'Merged video has not been generated'}</strong>
        {failed && <span role="alert">{label(merged?.status)}{merged?.error_code && ' · ' + label(merged.error_code)}</span>}
        <span>{!confirmed.length
          ? 'Confirm the correct candidates first, then wait for their clips and route recalculation.'
          : !clipsReady ? 'Waiting for all confirmed clips to be ready.'
          : requestExport ? 'Generate the merged video after completing your candidate review.'
          : 'An operator with case export permission can generate the merged video.'}</span>
        {requestExport && <>
          <button className="primary" disabled={busy || !clipsReady} onClick={requestExport}>
            {failed ? 'Retry merged video' : 'Generate merged video'}
          </button>
          <span>Export finalizes the case and ends candidate review.</span>
        </>}
      </>}
    </div>}
  </div>
}

export function Workspace({ view, client, cycle, requestExport, busy }: {
  view: CaseView; client?: Controls; cycle: number; requestExport?: () => void; busy?: boolean
}) {
  const recorded = ['CLOSED', 'SAVED', 'EXPORTING'].includes(view.lifecycle)
  const [tab, setTab] = useState<'cameras' | 'route'>(
    ['CLOSED', 'SAVED', 'EXPORTING'].includes(view.lifecycle ?? '') ? 'route' : 'cameras'
  )
  const frameCount = Number(Boolean(view.live?.stream_url))
  return <section className="panel monitor" aria-labelledby="monitor-title">
    <header className="panel-head">
      <div><h2 id="monitor-title">Monitoring workspace</h2><p>Controls API · Case media</p></div>
      <div className="view-switch" aria-label="Monitoring view">
        <button aria-pressed={tab === 'cameras'} onClick={() => setTab('cameras')}>
          {recorded ? <Film size={15}/> : <Camera size={15}/>} {recorded ? 'Merged video' : 'Live preview'}
        </button>
        <button aria-pressed={tab === 'route'} onClick={() => setTab('route')}><Map size={15}/> Route map</button>
      </div>
    </header>
    {tab === 'cameras' ? recorded
      ? <MergedVideoPanel view={view} client={client} requestExport={requestExport} busy={busy}/>
      : <CameraPanel view={view} client={client} cycle={cycle}/>
    : <RoutePanel view={view} client={client}/>}
    <div className="panel-foot"><span>{recorded ? 'Recorded case evidence' : <><i className={'dot ' + (frameCount ? 'green' : '')}/>{frameCount} {'published feed' + (frameCount === 1 ? '' : 's')}</>}</span><span>All times KST</span></div>
  </section>
}

export function ReviewQueue({ items, onPick }: { items: Candidate[]; onPick: (candidate: Candidate) => void }) {
  const [filter, setFilter] = useState('pending')
  const pending = items.filter(pendingReview)
  const visible = filter === 'pending' ? pending : items
  return <aside className="panel review" aria-labelledby="review-heading">
    <header className="panel-head"><div><h2 id="review-heading">Review queue</h2><p>{pending.length} awaiting review</p></div><div className="panel-icon"><Crosshair size={18}/></div></header>
    <div className="queue-filters" aria-label="Candidate filter">
      <button aria-pressed={filter === 'pending'} onClick={() => setFilter('pending')}>Pending <span>{pending.length}</span></button>
      <button aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>All <span>{items.length}</span></button>
    </div>
    <div className="queue-list">
      {visible.map((candidate) => <button className="candidate" onClick={() => onPick(candidate)} key={candidate.candidate_id}>
        <span className="candidate-icon"><Eye size={17}/></span>
        <span className="candidate-copy"><strong>{candidate.camera_id ?? 'Unknown camera'}</strong><small>{time(candidate.t_start ?? undefined)} · {pendingReview(candidate) ? 'Pending' : label(candidate.decision_state)}</small></span>
        <span className="match">{candidate.appearance_score == null ? 'Score unavailable' : candidate.appearance_score.toFixed(3)}</span>
        <ChevronRight size={15}/>
      </button>)}
      {visible.length === 0 && <p className="queue-empty">No {filter === 'pending' ? 'pending ' : ''}candidates.</p>}
    </div>
  </aside>
}

export function CaseVideo({ client, path, title, download }: {
  client: Controls; path: string; title: string; download?: string
}) {
  const [open, setOpen] = useState(false)
  return <details className="case-video" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{title}</summary>
    {open && <VideoPlayback key={path} client={client} path={path} title={title} download={download}/>}
  </details>
}

function VideoPlayback({ client, path, title, download }: {
  client: Controls; path: string; title: string; download?: string
}) {
  const { src, error } = useMedia(client, path, 'video/mp4')
  const [playbackError, setPlaybackError] = useState(false)
  if (error) return <p className="notice error" role="alert">{error}</p>
  if (!src) return <p role="status">Loading video…</p>
  return <>
    <video className="large-frame" src={src} controls playsInline aria-label={title}
      onError={() => setPlaybackError(true)}/>
    {playbackError && <p className="notice error" role="alert">This video could not be played.</p>}
    {download && <a className="case-video-download" href={src} download={download}>Download MP4</a>}
  </>
}

function CandidateThumbnail({ client, path }: { client: Controls; path?: string }) {
  const { src } = useMedia(client, path, 'image/*')
  return src ? <img className="candidate-thumbnail" src={src} alt="Candidate crop from Controls"/> : null
}

export function CandidateDialog({ candidate, clip, client, close, submit, canReview, busy, error }: {
  candidate: Candidate; clip?: Clip; client?: Controls; close: () => void; submit: (decision: Decision) => void;
  canReview: boolean; busy: boolean; error?: string
}) {
  return <Modal title="Candidate review" close={close}>
    {client && <CandidateThumbnail client={client} path={candidate.thumbnail_refs?.[0]}/>}
    <dl className="candidate-details">
      <div><dt>Candidate</dt><dd>{candidate.candidate_id}</dd></div>
      <div><dt>Camera</dt><dd>{candidate.camera_id ?? 'Unknown'}</dd></div>
      <div><dt>Observed</dt><dd>{time(candidate.t_start ?? undefined)} – {time(candidate.t_end ?? undefined)} KST</dd></div>
      <div><dt>Appearance similarity</dt><dd>{candidate.appearance_score == null ? 'Unavailable' : candidate.appearance_score.toFixed(3)} · ranking signal, not identity probability</dd></div>
      <div><dt>Decision state</dt><dd>{label(candidate.decision_state)}</dd></div>
    </dl>
    {client && (clip?.status === 'READY' && clip.url
      ? <CaseVideo client={client} path={clip.url} title="Candidate clip"/>
      : <p className="muted">Candidate clip · {clip ? label(clip.status) : 'Unavailable'}
        {clip?.error_code && ' · ' + label(clip.error_code)}</p>)}
    {error && <p role="alert" className="notice error">{error}</p>}
    {canReview && pendingReview(candidate) && <div className="dialog-actions">
      <button className="secondary" disabled={busy} onClick={() => submit('REJECT')}><X size={16}/> Reject</button>
      <button className="primary" disabled={busy} onClick={() => submit('CONFIRM')}><Check size={16}/> Confirm match</button>
    </div>}
  </Modal>
}

export function CaseProgress({ view }: { view: CaseView }) {
  const states = Object.entries(view.search_progress.jobs_by_state ?? {})
  return <section className="panel progress" aria-labelledby="progress-title">
    <header className="panel-head"><div><h2 id="progress-title">Search activity</h2><p>{view.stop_reason ? label(view.stop_reason) : view.search_progress.processing_message ?? 'Case processing and coverage'}</p></div><Activity size={18}/></header>
    <div className="progress-stats">
      <div><strong>{view.search_progress.jobs_total ?? '—'}</strong><span>Search jobs</span></div>
      <div><strong>{view.search_progress.processed_camera_seconds == null ? '—'
        : Math.round(view.search_progress.processed_camera_seconds)}</strong><span>Camera seconds processed</span></div>
      <div><strong>{view.unobserved_windows.length}</strong><span>Coverage gaps</span></div>
      <div><strong>{view.errors.length}</strong><span>Reported errors</span></div>
    </div>
    {(states.length > 0 || view.unobserved_windows.length > 0 || view.errors.length > 0) && <details>
      <summary>View activity details</summary>
      <ul>{states.map(([state, count]) => <li key={state}>{label(state)} · {count}</li>)}
      {view.errors.map((error, index) => <li key={'error-' + index}>{error.camera_id && error.camera_id + ' · '}{error.message ?? error.error ?? error.code ?? 'Backend reported an error'}</li>)}
      {view.unobserved_windows.map((gap, index) => <li key={'gap-' + index}>{gap.camera_id ?? 'Coverage gap'} · {gap.reason ?? 'Unobserved window'}
        {gap.t_from && ' · ' + time(gap.t_from) + ' KST'}
        {gap.t_to && (gap.t_to === 'OPEN' ? ' onward' : ' to ' + time(gap.t_to) + ' KST')}</li>)}</ul>
    </details>}
  </section>
}
