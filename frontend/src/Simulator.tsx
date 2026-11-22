import { useEffect, useState } from 'react'
import { Activity, Aperture, Camera, Check, ChevronRight, CircleAlert, Crosshair, FileDown, FolderKanban, LayoutGrid, LogOut, Map, MapPin, Minus, Plus, RotateCcw, Search, Timer } from 'lucide-react'
import { CaseIntake } from './CaseIntake'
import { DemoSignIn } from './DemoSignIn'
import { SearchStages } from './SearchStages'
import { Modal, time } from './Workspace'
import { RecordingWall, WarehousePlayer } from './RecordingWall'
import { WarehouseRoute } from './WarehouseRoute'
import casePoster from './warehouse/cam-002.jpg'

type Candidate = {
  id: string; camera_id: string; observed_at: string; appearance_score: number
  decision: string; review_state: string; requested_decision?: string; clip_state: string
  observation_type: string; explanation?: string; clip_url?: string
}
type Case = {
  id: string; title: string; lifecycle: string; mode: string; done: boolean; revision: number; route_revision: number
  source: string; status_label: string; stop_label?: string; can_review: boolean
  created_at: string; last_known: { camera_id: string; time: string }; notes: string
  reference_b64?: string; candidates: Candidate[]
  route: { camera_id: string; observed_at: string; source: string }[]
  errors: { code: string; message?: string; camera_id?: string }[]
  unobserved_windows: { camera_id?: string; start: string; end: string }[]
  merged_clip_url?: string
}

type DemoSession = { token: string; cameras: Record<string, string> }
type Section = 'find' | 'new' | 'current'
type Tab = 'Candidates' | 'Route' | 'Case details'
type Action = 'close' | 'delete'

async function request(path: string, token?: string, body?: object, method?: string, signal?: AbortSignal) {
  const headers = new Headers()
  if (token) headers.set('Authorization', 'Bearer ' + token)
  if (body) headers.set('Content-Type', 'application/json')
  const response = await fetch('/demo-api/' + path, {
    headers, method: method ?? (body ? 'POST' : 'GET'), body: body ? JSON.stringify(body) : undefined, signal,
  })
  if (!response.ok) {
    const problem = await response.json().catch(() => null)
    throw Error(problem?.detail ?? 'The demo simulator could not complete the request.')
  }
  return response
}

function message(error: unknown) {
  return error instanceof Error ? error.message : 'Please try again.'
}

function Intake({ session, complete }: { session: DemoSession; complete: (item: Case) => void }) {
  return <CaseIntake demo cameras={session.cameras} submit={async ({ title, camera, notes, known, photo }) => {
    const response = await request('cases', session.token, {
      title, camera, notes, known, no_photo: !photo,
      reference: photo?.src.split(',')[1], rotation: photo?.rotation, crop: photo?.crop,
    })
    complete(await response.json())
  }}/>
}

const warehouseCamera: Record<string, string> = { A: '001', B: '002', C: '003', D: '004' }

const safeMedia = (url?: string) => url && /^https?:\/\//i.test(url) ? url : undefined
const tabs: Tab[] = ['Candidates', 'Route', 'Case details']

export function Simulator() {
  const [session, setSession] = useState<DemoSession>()
  const [section, setSection] = useState<Section>('find')
  const [tab, setTab] = useState<Tab>('Candidates')
  const [mapZoom, setMapZoom] = useState(1)
  const [current, setCurrent] = useState<Case>()
  const [found, setFound] = useState<Case>()
  const [created, setCreated] = useState<Case>()
  const [picked, setPicked] = useState<string>()
  const [showRejected, setShowRejected] = useState(false)
  const [action, setAction] = useState<Action>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const currentId = current?.id
  const token = session?.token
  const pickedCandidate = current?.candidates.find((item) => item.id === picked)
  const latestSighting = current?.route.filter((point) => point.source === 'CONFIRMED').at(-1)
  const pending = current?.candidates.filter((item) => item.review_state === 'PENDING').length ?? 0
  const caseMetrics = current ? [
    { title: 'Candidates', value: current.candidates.length, Icon: Camera, tone: 'teal' },
    { title: 'Unreviewed', value: current.candidates.filter((item) => item.decision === 'UNREVIEWED' && item.review_state !== 'PENDING').length, Icon: Crosshair, tone: 'purple' },
    { title: 'Pending updates', value: pending, Icon: Timer, tone: 'amber' },
    { title: 'Confirmed', value: current.candidates.filter((item) => item.decision === 'CONFIRMED').length, Icon: Check, tone: 'green' },
  ] : []

  function open(item: Case) {
    setCurrent(item); setSection('current'); setTab('Candidates'); setError(''); setPollError(''); setShowRejected(false); setMapZoom(1)
  }

  async function run(task: () => Promise<void>) {
    setBusy(true); setError('')
    try { await task() }
    catch (failure) { setError(message(failure)) }
    finally { setBusy(false) }
  }

  useEffect(() => {
    if (section !== 'current' || !currentId || !token) return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next: Case = await (await request('cases/' + currentId, token, undefined, undefined, abort.signal)).json()
        if (!abort.signal.aborted) {
          setCurrent((previous) => previous?.id === next.id && next.revision >= previous.revision && next.route_revision >= previous.route_revision ? next : previous)
          setPollError('')
        }
      } catch (failure) { if (!abort.signal.aborted) setPollError(message(failure)) }
      finally { if (!abort.signal.aborted) timer = setTimeout(poll, 2000) }
    }
    void poll()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [section, currentId, token])

  const nav = [
    { value: 'find', name: 'Find case', Icon: Search },
    { value: 'new', name: 'New case', Icon: Plus },
    { value: 'current', name: 'Current case', Icon: LayoutGrid },
  ] as const

  const cameraName = (camera?: string) => camera ? 'Camera ' + camera + (session?.cameras[camera] ? ' · ' + session.cameras[camera] : '') : 'Camera unavailable'
  const casePath = 'cases/' + currentId
  const report = () => run(async () => {
    const pdf = await (await request(casePath + '/report', token)).blob()
    const url = URL.createObjectURL(pdf)
    const link = document.createElement('a')
    link.href = url; link.download = currentId + '.pdf'; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  })

  if (!session) return <DemoSignIn busy={busy} error={error} signIn={(username, password) => {
    void run(async () => setSession(await (await request('login', undefined, { username, password })).json()))
  }}/>

  return <div className="app-shell ops-shell trace-console">
    <a className="skip" href="#main">Skip to workspace</a>
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Aperture size={24}/></span><span><strong>TRACE</strong><small>OPERATIONS</small></span></div>
      <nav aria-label="Workspace navigation">
        {nav.map(({ value, name, Icon }) => <button key={value} aria-current={section === value ? 'page' : undefined} onClick={() => {
          setSection(value); setError(''); setPollError('')
          if (value === 'new') setCreated(undefined)
        }}><Icon size={18}/>{name}</button>)}
      </nav>
      <div className="sidebar-bottom"><span className="muted">Signed in as operator</span>
        <button className="help" disabled={busy} onClick={() => {
          setSession(undefined); setSection('find'); setCurrent(undefined); setFound(undefined); setCreated(undefined); setError('')
        }}><LogOut size={17}/> Sign out</button>
      </div>
    </aside>
    <main id="main" className="main">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><span>/</span><strong>{nav.find((item) => item.value === section)?.name}</strong></div><span className="mode-pill"><i className="dot"/>Demo mode</span></header>
      <div className="content">
        {error && !picked && !action && <p className="notice error" role="alert">{error}</p>}
        {pollError && <p className="notice error" role="alert">{pollError}</p>}
        {section === 'find' && <>
          <div className="page-head"><h1>Find case</h1></div>
          <div className="ops-find-grid">
          <section className="panel ops-lookup">
            <header className="ops-panel-header"><span className="ops-panel-icon"><Search size={18}/></span><div><h2>Saved cases</h2><p>Look up a case with its ID</p></div></header>
            <div className="simulator-panel">
            <form className="form" onSubmit={(event) => {
              event.preventDefault()
              const id = String(new FormData(event.currentTarget).get('id')).trim().toLowerCase()
              setFound(undefined)
              void run(async () => setFound(await (await request('cases/' + encodeURIComponent(id), token)).json()))
            }}>
              <label>Case ID<input name="id" placeholder="Enter a saved case ID" required/></label><button type="submit" disabled={busy} className="primary"><Search size={16}/> Find case</button>
            </form>
            {found && <div className="found-case"><p>{found.title} · {found.status_label}</p><button className="secondary" onClick={() => open(found)}>Open case</button></div>}
            </div>
          </section>
          <section className="panel ops-sample-card">
            <img src={casePoster} alt=""/>
            <div><div><h2>Demo case</h2><p>Explore the warehouse search workflow.</p></div><button className="secondary" disabled={busy} onClick={() => void run(async () => open(await (await request('cases', token, { sample: true })).json()))}><Plus size={16}/> Create demo case</button></div>
          </section>
          </div>
        </>}
        {section === 'new' && <>
          <div className="page-head"><h1>New case</h1></div>
          {created ? <div className="panel ops-created"><span className="ops-panel-icon"><Check size={20}/></span><div><h2>Case created</h2><p role="status">Case ID <code>{created.id}</code></p></div><button className="primary" onClick={() => open(created)}>Open case <ChevronRight size={16}/></button></div>
            : <Intake session={session} complete={setCreated}/>}
        </>}
        {section === 'current' && (current ? <>
          <header className="ops-case-header">
            <div>
              <div className="case-kicker"><Crosshair size={15}/><span>{current.id}</span></div>
              <h1>{current.title}</h1>
              <p><MapPin size={15}/> {cameraName(current.last_known.camera_id)} <span className="ops-divider">·</span> Reported at {time(current.last_known.time)} KST</p>
            </div>
            <div className={current.done ? 'ops-case-status closed' : 'ops-case-status'}>
              <Activity size={15}/><span>{current.status_label}</span>
              {current.stop_label && <small>{current.stop_label}</small>}
            </div>
          </header>
          <SearchStages key={current.id} mode={current.mode} lifecycle={current.lifecycle} done={current.done}/>
          {current.mode === 'LIVE_TRACKING' && <p className="notice">The demo simulates live tracking. Use the candidate clips to review sightings.</p>}
          {(current.mode === 'WAITING' || current.done) && latestSighting && <p className="notice">
            Last confirmed sighting · {cameraName(latestSighting.camera_id)} · {time(latestSighting.observed_at)} KST
          </p>}
          <div className="ops-metrics">
            {caseMetrics.map(({ title, value, Icon, tone }) => <div key={title} className="ops-metric">
              <span className={'ops-metric-icon ' + tone}><Icon size={18}/></span>
              <span><small>{title}</small><strong>{value}</strong></span>
            </div>)}
          </div>
          {!!pending && <p className="notice success" role="status">Decision submitted. Updating results.</p>}
          <nav className="ops-tabs" aria-label="Case view">
            {tabs.map((name, index) => {
              const Icon = [LayoutGrid, Map, FolderKanban][index]
              return <button key={name} aria-pressed={tab === name} onClick={() => setTab(name)}><Icon size={17}/>{name}</button>
            })}
          </nav>
          {tab === 'Candidates' && <div className="ops-workspace">
            <RecordingWall/>
            <section className="panel ops-review" aria-labelledby="ops-review-title">
              <header className="ops-panel-header">
                <span className="ops-panel-icon"><Crosshair size={18}/></span>
                <div><h2 id="ops-review-title">Review queue</h2><p>{caseMetrics[1]?.value ?? 0} unreviewed</p></div>
              </header>
              <label className="check-row ops-review-filter"><input type="checkbox" checked={showRejected} onChange={(e) => setShowRejected(e.target.checked)}/>Show rejected candidates</label>
              <div className="ops-review-list">
                {current.candidates.filter((item) => item.decision !== 'REJECTED' || showRejected).map((item) => <button type="button" className="ops-review-item" key={item.id} onClick={() => { setPicked(item.id); setError('') }}>
                  <span className="ops-candidate-camera"><Camera size={16}/></span>
                  <span className="ops-candidate-copy"><strong>{cameraName(item.camera_id)}</strong><small>{time(item.observed_at)} KST</small>
                    <span className={'ops-review-state ' + item.decision.toLowerCase()}>{item.review_state === 'PENDING' ? 'Updating' : item.decision.toLowerCase()}</span>
                  </span>
                  <span className="match">{item.appearance_score.toFixed(2)}</span><ChevronRight size={15}/>
                </button>)}
                {!current.candidates.some((item) => showRejected || item.decision !== 'REJECTED') && <div className="ops-review-empty">
                  <Crosshair size={24}/><p>{current.candidates.length ? 'No visible candidates.' : current.done ? 'No candidates found.' : 'Waiting for candidates'}</p>
                </div>}
              </div>
              {(current.errors.length > 0 || current.unobserved_windows.length > 0) && <button className="ops-coverage" onClick={() => setTab('Case details')}><CircleAlert size={17}/> Review coverage issues<ChevronRight size={15}/></button>}
            </section>
          </div>}
          {tab === 'Route' && <div className="ops-workspace">
            <section className="panel ops-route">
              <header className="ops-panel-header"><span className="ops-panel-icon"><Map size={18}/></span>
                <div><h2>{current.done ? 'Final route' : 'Case route'}</h2><p>Warehouse floor plan · Simulated sightings</p></div>
                <span className="zoom" aria-label="Map zoom controls">
                  <button className="icon-button" type="button" aria-label="Zoom out" disabled={mapZoom <= 1} onClick={() => setMapZoom((value) => value - .25)}><Minus size={15}/></button>
                  <button className="icon-button" type="button" aria-label="Reset zoom" disabled={mapZoom === 1} onClick={() => setMapZoom(1)}><RotateCcw size={14}/></button>
                  <button className="icon-button" type="button" aria-label="Zoom in" disabled={mapZoom >= 2.5} onClick={() => setMapZoom((value) => value + .25)}><Plus size={15}/></button>
                </span>
              </header>
              <div className="ops-map-viewport" tabIndex={mapZoom > 1 ? 0 : undefined} aria-label="Route map viewport">
                <WarehouseRoute route={current.route} zoom={mapZoom}/>
              </div>
              {safeMedia(current.merged_clip_url) && <video className="large-frame" controls src={safeMedia(current.merged_clip_url)}/>}
            </section>
            <section className="panel ops-review">
              <header className="ops-panel-header"><span className="ops-panel-icon"><MapPin size={18}/></span><div><h2>Sightings</h2><p>{current.route.length} route points</p></div></header>
              {current.route.length ? <ol className="ops-timeline">{current.route.map((point, index) => <li key={index}><span className="route-step">{index + 1}</span>
                <strong>{cameraName(point.camera_id)}</strong><small>{time(point.observed_at)} KST · {point.source === 'REPORTED' ? 'Reported by staff' : 'Confirmed'}</small>
              </li>)}</ol> : <p className="simulator-panel">No route available yet.</p>}
            </section>
          </div>}
          <div className="panel" hidden={tab !== 'Case details'}>
            <div className="simulator-panel">
              {tab === 'Case details' && <>
                {current.reference_b64 && <img className="reference-preview" alt="Reference photo" src={'data:image/jpeg;base64,' + current.reference_b64}/>}
                <dl className="candidate-details">
                  <div><dt>Case ID</dt><dd>{current.id}</dd></div><div><dt>Last-known location</dt><dd>{cameraName(current.last_known.camera_id)}</dd></div>
                  <div><dt>Last seen</dt><dd>{new Date(current.last_known.time).toLocaleDateString('en-CA', { timeZone: 'Asia/Seoul' })} · {time(current.last_known.time)} KST</dd></div>
                  <div><dt>Created</dt><dd>{new Date(current.created_at).toLocaleDateString('en-CA', { timeZone: 'Asia/Seoul' })} · {time(current.created_at)} KST</dd></div>
                  {current.notes && <div><dt>Notes</dt><dd>{current.notes}</dd></div>}
                </dl>
                <details><summary>Coverage issues</summary>
                  {current.errors.map((item, index) => <p key={index}>{item.camera_id ? cameraName(item.camera_id) + ' · ' : ''}{item.message || item.code}</p>)}
                  {current.unobserved_windows.map((item, index) => <p key={index}>{cameraName(item.camera_id)} · {time(item.start)}–{time(item.end)} KST</p>)}
                  {!current.errors.length && !current.unobserved_windows.length && <p>No coverage issues reported.</p>}
                </details>
                <div className="head-actions found-case">
                  <button className="secondary" disabled={busy} onClick={() => void report()}><FileDown size={16}/> Download report</button>
                  {['PREPARING', 'SEARCHING'].includes(current.lifecycle) && !current.done && <button className="secondary" disabled={busy || !!pending} onClick={() => setAction('close')}>Close search</button>}
                  {(current.done || current.lifecycle === 'FAILED') && <button className="secondary" disabled={busy} onClick={() => setAction('delete')}>Delete case</button>}
                </div>
              </>}
            </div>
          </div>
        </> : <div className="panel empty"><FolderKanban size={28}/><h1>Current case</h1><p>Find or create a case to continue.</p></div>)}
      </div>
    </main>
    {current && pickedCandidate && <Modal title={cameraName(pickedCandidate.camera_id)} close={() => { setPicked(undefined); setError('') }}>
      <p>{pickedCandidate.id} · {time(pickedCandidate.observed_at)} KST · Appearance score {pickedCandidate.appearance_score.toFixed(2)}</p>
      {pickedCandidate.observation_type === 'LONG_GAP' ? <div className="notice candidate-gap">
        <CircleAlert size={18} aria-hidden="true"/>
        <div><strong>Long gap between sightings</strong>
          {current.can_review && pickedCandidate.decision === 'UNREVIEWED' && pickedCandidate.review_state !== 'PENDING' &&
            <p>Confirm the match to add this sighting to the main route.</p>}
        </div>
      </div> : pickedCandidate.explanation && pickedCandidate.explanation !== 'Simulated observation supplied by the control API.' &&
        <p>{pickedCandidate.explanation}</p>}
      <details><summary>{current.source === 'demo' ? 'Warehouse sample clip' : 'Video'}</summary>{current.source === 'demo' ? <>
        <p className="muted">Warehouse camera {warehouseCamera[pickedCandidate.camera_id]} · 3-second sample. The box marks the demo candidate.</p>
        <WarehousePlayer variant="clip" key={pickedCandidate.camera_id} camera={warehouseCamera[pickedCandidate.camera_id]}/>
      </>
        : safeMedia(pickedCandidate.clip_url) ? <video className="large-frame" controls src={safeMedia(pickedCandidate.clip_url)}/>
        : <p>Video is not available yet.</p>}</details>
      {error && <p role="alert" className="notice error">{error}</p>}
      {pickedCandidate.review_state === 'PENDING' ? <p>{pickedCandidate.requested_decision} submitted · updating results</p>
        : pickedCandidate.decision !== 'UNREVIEWED' ? <p>{pickedCandidate.decision} · Clip {pickedCandidate.clip_state}</p>
        : <div className="dialog-actions">{['REJECT', 'CONFIRM'].map((decision) => <button key={decision} className={decision === 'CONFIRM' ? 'primary' : 'secondary'} disabled={busy || !current.can_review} onClick={() => void run(async () => {
          const next = await (await request(casePath + '/decision', token, { candidate: pickedCandidate.id, decision })).json()
          setCurrent(next); setPicked(undefined)
        })}>{decision === 'CONFIRM' && <Check size={16}/>} {decision === 'CONFIRM' ? 'Confirm' : 'Reject'}</button>)}</div>}
    </Modal>}
    {current && action && <Modal title={action === 'close' ? 'Close search?' : 'Delete case'} close={() => { setAction(undefined); setError('') }}>
      <form onSubmit={(event) => {
        event.preventDefault()
        void run(async () => {
          if (action === 'close') setCurrent(await (await request(casePath + '/close', token, {})).json())
          else { await request(casePath, token, undefined, 'DELETE'); setCurrent(undefined); setSection('find'); setFound(undefined) }
          setAction(undefined)
        })
      }}>
        {action === 'delete' && <label className="check-row"><input type="checkbox" required/>Permanently delete this case</label>}
        {error && <p className="notice error" role="alert">{error}</p>}
        <div className="dialog-actions">
          {action === 'close' && <button type="button" className="secondary" disabled={busy} onClick={() => { setAction(undefined); setError('') }}>Cancel</button>}
          <button type="submit" className="primary" disabled={busy}>{action === 'close' ? (busy ? 'Closing…' : 'Confirm') : 'Delete case'}</button>
        </div>
      </form>
    </Modal>}
  </div>
}
