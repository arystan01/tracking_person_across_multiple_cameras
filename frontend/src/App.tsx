import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Activity, Aperture, ArrowUpRight, Bell, Check, ChevronsUpDown, CircleHelp, Crosshair, FolderKanban, LayoutGrid, LogIn, LogOut, Plus, Radio, RefreshCw, Search, ShieldCheck } from 'lucide-react'
import { allowed, Controls, ControlsError, type Candidate, type CaseSummary, type CaseView, type Command, type Decision, type Session } from './controls'
import { CaseIntake, preparePhoto } from './CaseIntake'
import { SearchStages } from './SearchStages'
import { demoSummary, demoView } from './demo'
import { CandidateDialog, CaseProgress, CaseVideo, label, Modal, pendingReview, ReviewQueue, time, Workspace } from './Workspace'

type Screen = 'monitoring' | 'cases'
const commandLabels: Record<Command, string> = { CLOSE: 'Close search', EXPORT: 'Request clip export', DELETE: 'Delete case' }
const operationError = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Try again.'

function SignIn({ session, done, close }: { session?: Session; done: (session?: Session) => void; close: () => void }) {
  const passwordChange = session?.user.must_change_password
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const fields = new FormData(event.currentTarget)
    setBusy(true)
    setError('')
    try {
      const controls = new Controls(session?.access_token)
      if (passwordChange) {
        await controls.changePassword(String(fields.get('password')), String(fields.get('new_password')))
        done()
      } else {
        done(await controls.login(String(fields.get('username')).trim(), String(fields.get('password'))))
      }
    } catch (failure) {
      setError(operationError(failure))
    } finally {
      setBusy(false)
    }
  }
  return <Modal title={passwordChange ? 'Change temporary password' : 'Connect to Controls'} close={close}>
    <form className="form" onSubmit={submit}>
      <p className="muted">{passwordChange ? 'Set a new password and sign in again.' : 'Sign in with your JAVES-AI account to open backend cases.'}</p>
      {!passwordChange && <label>Username<input name="username" autoComplete="username" required/></label>}
      <label>{passwordChange ? 'Current password' : 'Password'}<input name="password" type="password" autoComplete="current-password" required/></label>
      {passwordChange && <label>New password<input name="new_password" type="password" autoComplete="new-password" minLength={12} required/></label>}
      {error && <p role="alert" className="notice error">{error}</p>}
      <button className="primary" disabled={busy} type="submit"><LogIn size={16}/>{busy ? 'Connecting…' : passwordChange ? 'Change password' : 'Sign in'}</button>
    </form>
  </Modal>
}

const siteId = import.meta.env.VITE_TRACE_SITE_ID?.trim()
const siteVersion = import.meta.env.VITE_TRACE_SITE_VERSION?.trim()

function configuredCameras(): Record<string, string> {
  if (!siteId || !siteVersion) return {}
  try {
    const cameras: unknown = JSON.parse(import.meta.env.VITE_TRACE_CAMERAS ?? '{}')
    if (!cameras || typeof cameras !== 'object' || Array.isArray(cameras)) return {}
    return Object.fromEntries(Object.entries(cameras).filter(([id, name]) =>
      id.trim() && typeof name === 'string' && name.trim()))
  } catch {
    return {}
  }
}
const cameras = configuredCameras()

function NewCase({ close, client, session, created, expired }: {
  close: () => void
  client: Controls
  session: Session
  created: (view: CaseView) => void
  expired: (error: unknown) => boolean
}) {
  const [busy, setBusy] = useState(false)
  const [caseId] = useState(() => 'case-' + crypto.randomUUID())
  const [targetId] = useState(() => 'target-' + crypto.randomUUID())
  const upload = useRef<{ src: string; edits: string; file: File }>(undefined)

  return <Modal title="New case" className="intake-dialog" close={() => { if (!busy) close() }}>
    <CaseIntake cameras={cameras} submit={async ({ camera, known, photo }) => {
      if (!photo || !siteId || !siteVersion) return
      setBusy(true)
      try {
        const edits = JSON.stringify([photo.rotation, photo.crop])
        if (upload.current?.src !== photo.src || upload.current.edits !== edits) {
          upload.current = { src: photo.src, edits, file: await preparePhoto(photo) }
        }
        const reference = await client.stageReference(caseId, upload.current.file)
        created(await client.create({
          case_id: caseId, target_id: targetId, site_id: siteId, site_version: siteVersion,
          reference_image_ref: reference,
          last_known: { camera_id: camera, time: new Date(known).toISOString() },
        }, session.user))
      } catch (error) {
        if (!expired(error)) throw error
      } finally {
        setBusy(false)
      }
    }}/>
  </Modal>
}

export function App() {
  const [session, setSession] = useState<Session>()
  const [screen, setScreen] = useState<Screen>('monitoring')
  const [cases, setCases] = useState<CaseSummary[]>([])
  const [caseId, setCaseId] = useState<string>()
  const [view, setView] = useState<CaseView>()
  const [sample, setSample] = useState(demoView)
  const [dialog, setDialog] = useState<'auth' | 'create' | 'help' | Command>()
  const [picked, setPicked] = useState<string>()
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const [caseListError, setCaseListError] = useState('')
  const [status, setStatus] = useState('')
  const [query, setQuery] = useState('')
  const [cycle, setCycle] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const client = useMemo(() => session ? new Controls(session.access_token) : undefined, [session])
  const demo = !session
  const user = session?.user
  const active = demo ? sample : view
  const selected = demo ? demoSummary : cases.find((item) => item.case_id === caseId)
  const pickedCandidate = active?.candidates.find((item) => item.candidate_id === picked)

  function resetSession(newSession?: Session) {
    setSession(newSession); setCaseId(undefined); setView(undefined); setCases([]); setPicked(undefined)
    setActionError(''); setConnectionError(''); setCaseListError(''); setStatus('')
    setScreen(newSession ? 'cases' : 'monitoring')
    setDialog(newSession?.user.must_change_password ? 'auth' : undefined)
  }

  const apiError = useCallback((error: unknown) => {
    if (error instanceof ControlsError && error.status === 401) {
      resetSession()
      setConnectionError('Your session expired. Sign in to continue.')
      setDialog('auth')
      return true
    }
    return false
  }, [])

  useEffect(() => {
    if (!client || user?.must_change_password) return
    const abort = new AbortController()
    setCaseListError('')
    client.cases(abort.signal).then(setCases).catch((error: unknown) => {
      if (!abort.signal.aborted && !apiError(error)) setCaseListError(operationError(error))
    })
    return () => abort.abort()
  }, [client, user, refresh, apiError])

  const acceptView = useCallback((next: CaseView) => {
    setView((prev) => {
      if (!prev || prev.case_id !== next.case_id) return next
      return next.revision >= prev.revision && next.route_revision >= prev.route_revision ? next : prev
    })
  }, [])

  useEffect(() => {
    if (!client || !caseId || screen !== 'monitoring' || user?.must_change_password) return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next = await client!.view(caseId!, abort.signal)
        if (!abort.signal.aborted) {
          acceptView(next); setConnectionError(''); setCycle((value) => value + 1)
        }
      } catch (error) {
        if (!abort.signal.aborted && !apiError(error)) setConnectionError(operationError(error))
      } finally {
        if (!abort.signal.aborted) timer = setTimeout(poll, 5000)
      }
    }
    void poll()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [client, caseId, screen, user, refresh, acceptView, apiError])

  function openCase(id: string) {
    if (caseId !== id) setView(undefined)
    setCaseId(id); setConnectionError(''); setStatus(''); setScreen('monitoring')
  }

  function openDialog(next: typeof dialog) {
    setActionError('')
    setDialog(next)
  }

  async function signOut() {
    if (!client || busy) return
    setBusy(true)
    try {
      await client.logout()
      resetSession()
    } catch (error) {
      if (!apiError(error)) setConnectionError(operationError(error))
    } finally { setBusy(false) }
  }

  async function decide(decision: Decision) {
    if (!active || !pickedCandidate || busy) return
    setBusy(true); setActionError('')
    try {
      if (demo) {
        setSample({ ...sample, candidates: sample.candidates.map((item) => item.candidate_id === picked
          ? { ...item, review_state: decision === 'CONFIRM' ? 'CONFIRMED' : 'REJECTED' } : item) })
        setStatus(decision === 'CONFIRM' ? 'Demo match confirmed.' : 'Demo candidate rejected.')
      } else if (client && user) {
        acceptView(await client.decide(active.case_id, pickedCandidate, decision, user))
        setStatus('Decision submitted to Controls.')
        setRefresh((value) => value + 1)
      }
      setPicked(undefined)
    } catch (error) {
      if (!apiError(error)) setActionError(operationError(error))
    } finally { setBusy(false) }
  }

  async function command(action: Command) {
    if (!active || busy) return
    setBusy(true); setActionError('')
    try {
      if (demo && action === 'CLOSE') setSample({ ...sample, lifecycle: 'CLOSED', mode: null, done: true, stop_reason: 'OPERATOR_CLOSED' })
      if (client && user) {
        const next = await client.command(active.case_id, action, user)
        if (action === 'DELETE') {
          setCaseId(undefined); setView(undefined); setScreen('cases')
        } else {
          acceptView(next)
        }
        setRefresh((value) => value + 1)
      }
      setStatus(commandLabels[action] + ' submitted.')
      setDialog(undefined)
    } catch (error) {
      if (!apiError(error)) setActionError(operationError(error))
    } finally { setBusy(false) }
  }

  const lifecycle = active?.lifecycle
  const retained = ['CLOSED', 'EXPORTING', 'SAVED'].includes(lifecycle ?? '')
  const confirmedClips = retained ? (active?.clips ?? []).filter((clip) => clip.state === 'CONFIRMED') : []
  const actions: Command[] = []
  if ((demo || allowed(user, 'case.close')) && (lifecycle === 'PREPARING' || lifecycle === 'SEARCHING')) actions.push('CLOSE')
  if (allowed(user, 'case.export') && ['CLOSED', 'SAVED'].includes(lifecycle ?? '')) actions.push('EXPORT')
  if (allowed(user, 'case.delete') && ['CLOSED', 'SAVED', 'FAILED'].includes(lifecycle ?? '')) actions.push('DELETE')
  const visibleCases = (demo ? [{ ...demoSummary, lifecycle: sample.lifecycle, mode: sample.mode }] : cases)
    .filter((item) => [item.case_id, item.target_id, item.site_id, item.lifecycle].some((text) => text.toLowerCase().includes(query.toLowerCase().trim())))
  const canReview = (demo || allowed(user, 'case.review')) && ['SEARCHING', 'CLOSED'].includes(lifecycle ?? '') && (!demo || lifecycle !== 'CLOSED')

  return <div className="app-shell trace-console">
    <a href="#main" className="skip">Skip to workspace</a>
    <aside className="sidebar">
      <button className="brand" onClick={() => setScreen('monitoring')} aria-label="TRACE monitoring"><span className="brand-mark"><Aperture size={24}/></span><span><strong>TRACE</strong><small>OPERATIONS</small></span></button>
      <div className="workspace-id"><span className="site-mark"><ShieldCheck size={17}/></span><span><strong>{selected?.site_id ?? 'JAVES-AI'}</strong><small>{demo ? 'Demo workspace' : 'Connected workspace'}</small></span><ChevronsUpDown size={14}/></div>
      <nav aria-label="Workspace navigation">
        <span className="nav-label">Workspace</span>
        <button aria-current={screen === 'monitoring' ? 'page' : undefined} onClick={() => setScreen('monitoring')}><LayoutGrid size={18}/> Monitoring</button>
        <button aria-current={screen === 'cases' ? 'page' : undefined} onClick={() => setScreen('cases')}><FolderKanban size={18}/> Cases</button>
      </nav>
      <div className="sidebar-bottom">
        <button className="help" onClick={() => openDialog('help')}><CircleHelp size={17}/> Workspace help</button>
        <div className="profile"><span className="avatar">{(user?.username || 'Demo operator').slice(0, 2).toUpperCase()}</span><span><strong>{user?.username ?? 'Demo operator'}</strong><small>{demo ? 'Preview access' : 'Signed in'}</small></span>
          {!demo && <button className="icon-button" aria-label="Sign out" disabled={busy} onClick={() => void signOut()}><LogOut size={17}/></button>}
        </div>
      </div>
    </aside>
    <main id="main" className="main">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><span>/</span><strong>{screen === 'monitoring' ? 'Monitoring' : 'Cases'}</strong></div>
        <div className="topbar-actions">
          <span className={'mode-pill ' + (demo ? '' : 'connected')}><i className="dot"/>{demo ? 'Demo mode' : 'Controls connected'}</span>
          {demo ? <button className="secondary connect" onClick={() => openDialog('auth')}><LogIn size={16}/> Connect to Controls</button>
            : <button className="icon-button" aria-label="Refresh workspace" onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={17}/></button>}
        </div>
      </header>
      <div className="content">
        {connectionError && <p className="notice error" role="alert">{connectionError}</p>}
        {status && <p className="notice success" role="status"><Check size={16}/>{status}</p>}
        {user?.must_change_password ? <div className="panel empty"><h1>Complete account setup</h1><button className="primary" onClick={() => openDialog('auth')}>Change temporary password</button></div>
        : screen === 'cases' ? <>
          <div className="page-head"><div><h1>Search cases</h1><p>Open a case to view its route, camera frames and candidates.</p></div>
            {allowed(user, 'case.create') && <button className="primary" onClick={() => openDialog('create')}><Plus size={16}/> New case</button>}</div>
          <section className="panel cases-panel"><label className="search"><Search size={17}/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search cases" aria-label="Search cases"/></label>
            {caseListError && <p className="notice error" role="alert">{caseListError}</p>}
            {visibleCases.map((item) => <button className="case-row" onClick={() => openCase(item.case_id)} key={item.case_id}><FolderKanban size={20}/>
              <span><strong>{item.target_id}</strong><small>{item.case_id} · {item.site_id}</small></span><span className="tag">{label(item.lifecycle)}</span><ArrowUpRight size={17}/></button>)}
            {!visibleCases.length && !caseListError && <p className="muted">No cases match this view.</p>}
          </section>
        </> : active ? <>
          <div className="page-head">
            <div><div className="case-kicker"><Crosshair size={15}/>{active.case_id}<span className="tag">{label(active.lifecycle)}</span></div>
              <h1>Case monitoring</h1><p>{selected?.target_id ?? active.case_id}{selected?.site_id && ' · ' + selected.site_id}</p></div>
            <div className="head-actions">
              <button className="secondary" onClick={() => setScreen('cases')}><FolderKanban size={16}/> All cases</button>
              {actions.length > 0 && <select className="secondary" aria-label="Case actions" value="" onChange={(event) => openDialog(event.target.value as Command)}>
                <option value="" disabled>Case actions</option>{actions.map((action) => <option key={action} value={action}>{commandLabels[action]}</option>)}
              </select>}
            </div>
          </div>
          <SearchStages key={active.case_id} mode={active.mode} lifecycle={active.lifecycle} done={active.done}/>
          <div className="case-strip">
            <div><span className="strip-icon lime"><Radio size={17}/></span><span><small>Search mode</small><strong>{label(active.mode ?? active.lifecycle)}</strong></span></div>
            <div><span className="strip-icon purple"><Crosshair size={17}/></span><span><small>Pending review</small><strong>{active.candidates.filter(pendingReview).length} candidates</strong></span></div>
            <div><span className="strip-icon amber"><Bell size={17}/></span><span><small>Coverage gaps</small><strong>{active.unobserved_windows.length} reported</strong></span></div>
          </div>
          <div className="operations">
            <Workspace key={active.case_id} view={active} demo={demo} client={client} cycle={cycle}/>
            <ReviewQueue key={active.case_id} items={active.candidates} onPick={(candidate) => { setPicked(candidate.candidate_id); setActionError('') }}/>
          </div>
          <CaseProgress view={active}/>
          {(confirmedClips.length > 0 || active.export?.status) && <section className="panel case-export" aria-labelledby="case-export-heading">
            <h2 id="case-export-heading">Case recordings</h2>
            {client && confirmedClips.map((clip) => {
              const title = [clip.camera_id ?? 'Confirmed clip', clip.t_start && time(clip.t_start) + ' KST'].filter(Boolean).join(' · ')
              return clip.status === 'READY' && clip.url
                ? <CaseVideo key={clip.clip_id} client={client} path={clip.url} title={title}/>
                : <p key={clip.clip_id}>{title} · {label(clip.status)}</p>
            })}
            {client && active.export?.status === 'READY' && active.export.url
              ? <CaseVideo key={active.export.export_request_id} client={client} path={active.export.url} title="Play merged clip" download={active.case_id + '.mp4'}/>
              : active.export?.status && <p className="muted">Merged clip · {label(active.export.status)}
                {active.export.error_code && ' · ' + label(active.export.error_code)}</p>}
          </section>}
        </> : <div className="panel empty"><Activity size={30}/><h1>{caseId ? 'Loading case' : 'Select a search case'}</h1><p className="muted">{caseId ? caseId : 'Choose a case from the workspace to begin monitoring.'}</p>
          <button className="secondary" onClick={() => setScreen('cases')}><FolderKanban size={16}/> Browse cases</button></div>}
      </div>
    </main>
    {dialog === 'auth' && <SignIn key={session?.access_token ?? 'sign-in'} session={session} close={() => openDialog(undefined)} done={(value) => {
      resetSession(value)
      if (!value) { setStatus('Password changed. Sign in with your new password.'); setDialog('auth') }
    }}/>}
    {dialog === 'create' && client && session && <NewCase client={client} session={session}
      close={() => openDialog(undefined)} expired={apiError} created={(created) => {
        openCase(created.case_id)
        acceptView(created)
        setDialog(undefined)
        setRefresh((value) => value + 1)
      }}/>}
    {dialog === 'help' && <Modal title="Workspace help" close={() => openDialog(undefined)}><p className="muted">Demo mode uses fictional still images and an illustrative route map. Connect to your Controls server to access backend cases and the latest published camera frame.</p></Modal>}
    {dialog && dialog in commandLabels && <Modal title={commandLabels[dialog as Command]} close={() => openDialog(undefined)}>
      <p className="muted">Apply this action to {active?.case_id}?</p>{actionError && <p className="notice error" role="alert">{actionError}</p>}
      <div className="dialog-actions"><button className="secondary" onClick={() => openDialog(undefined)}>Cancel</button><button className="primary" disabled={busy} onClick={() => void command(dialog as Command)}>Confirm</button></div>
    </Modal>}
    {pickedCandidate && <CandidateDialog candidate={pickedCandidate} client={client}
      clip={active?.clips?.find((clip) => clip.clip_id === pickedCandidate.clip_id || clip.candidate_id === pickedCandidate.candidate_id)}
      close={() => setPicked(undefined)} submit={(decision) => void decide(decision)} canReview={canReview} busy={busy} error={actionError}/>}
  </div>
}
