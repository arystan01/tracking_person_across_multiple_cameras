import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Activity, Aperture, ArrowUpRight, Bell, Check, ChevronsUpDown, CircleHelp, Crosshair, FolderKanban, LayoutGrid, LogOut, Plus, Radio, RefreshCw, Search, ShieldCheck, Users } from 'lucide-react'
import { allowed, Controls, ControlsError, type Candidate, type CaseSummary, type CaseView, type Command, type Decision, type Session } from './controls'
import { Accounts } from './Accounts'
import { CaseIntake, preparePhoto } from './CaseIntake'
import { SearchStages } from './SearchStages'
import { SignIn, SignInLayout } from './SignIn'
import { CandidateDialog, CaseProgress, CaseVideo, label, Modal, pendingReview, ReviewQueue, time, Workspace } from './Workspace'

type Screen = 'monitoring' | 'cases' | 'accounts'
const screenLabels: Record<Screen, string> = { monitoring: 'Monitoring', cases: 'Cases', accounts: 'Accounts' }
const commandLabels: Record<Command, string> = { CLOSE: 'Close search', EXPORT: 'Request clip export', DELETE: 'Delete case' }
const operationError = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Try again.'

function PasswordChange({ client, done, signOut }: { client: Controls; done: () => void; signOut: () => void }) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const form = event.currentTarget
    const fields = new FormData(form)
    form.reset()
    setBusy(true)
    setError('')
    try {
      await client.changePassword(String(fields.get('password')), String(fields.get('new_password')))
      done()
    } catch (failure) {
      setError(operationError(failure))
    } finally {
      setBusy(false)
    }
  }
  return <SignInLayout title="Change temporary password" description="Set a new password of at least 12 characters, then sign in again.">
    <form className="form password-change-form" onSubmit={submit} aria-busy={busy}>
      <label>Current password<input name="password" type="password" autoComplete="current-password" disabled={busy} required/></label>
      <label>New password<input name="new_password" type="password" autoComplete="new-password" minLength={12} maxLength={1024} disabled={busy} required/></label>
      {error && <p role="alert" className="notice error">{error}</p>}
      <div className="dialog-actions">
        <button className="secondary" disabled={busy} type="button" onClick={signOut}>Sign out</button>
        <button className="primary" disabled={busy} type="submit">{busy ? 'Changing…' : 'Change password'}</button>
      </div>
    </form>
  </SignInLayout>
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

function NewCase({ close, client, user, created }: {
  close: () => void
  client: Controls
  user: Session['user']
  created: (view: CaseView) => void
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
        }, user))
      } finally {
        setBusy(false)
      }
    }}/>
  </Modal>
}

export function App() {
  const [session, setSession] = useState<Session>()
  const activeToken = useRef<string | undefined>(undefined)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const clearSession = useCallback((token: string, message: string, expired = false) => {
    if (activeToken.current !== token) return
    activeToken.current = undefined
    setSession(undefined)
    setError(expired ? message : '')
    setNotice(expired ? '' : message)
  }, [])

  const client = useMemo(() => {
    if (!session) return undefined
    const token = session.access_token
    return new Controls(token, () => clearSession(token, 'Your session expired. Sign in to continue.', true))
  }, [session, clearSession])

  async function signIn(username: string, password: string) {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const { access_token } = await new Controls().login(username.trim(), password)
      const signedIn = new Controls(access_token)
      try {
        const user = await signedIn.me()
        activeToken.current = access_token
        setSession({ access_token, user })
      } catch (failure) {
        void signedIn.logout().catch(() => {})
        throw failure
      }
    } catch (failure) {
      setError(operationError(failure))
    } finally {
      setBusy(false)
    }
  }

  async function signOut() {
    if (!session || !client || busy) return
    clearSession(session.access_token, '')
    setBusy(true)
    try {
      await client.logout()
      setNotice('Signed out.')
    } catch (failure) {
      if (failure instanceof ControlsError && failure.status === 401) setNotice('Signed out.')
      else setError('Signed out of this dashboard. Controls could not confirm that the session ended.')
    } finally {
      setBusy(false)
    }
  }

  if (!session || !client) return <SignIn signIn={signIn} busy={busy} error={error} notice={notice}/>
  if (session.user.must_change_password) return <PasswordChange client={client} signOut={signOut}
    done={() => clearSession(session.access_token, 'Password changed. Sign in with your new password.')}/>
  return <ConnectedWorkspace user={session.user} client={client} signOut={signOut}/>
}

function ConnectedWorkspace({ user, client, signOut }: { user: Session['user']; client: Controls; signOut: () => void }) {
  const canRead = allowed(user, 'case.read')
  const canManageAccounts = user.role === 'ROOT' && allowed(user, 'user.read')
  const [screen, setScreen] = useState<Screen>(canRead ? 'cases' : canManageAccounts ? 'accounts' : 'monitoring')
  const [cases, setCases] = useState<CaseSummary[]>([])
  const [caseId, setCaseId] = useState<string>()
  const [active, setView] = useState<CaseView>()
  const [dialog, setDialog] = useState<'create' | 'help' | Command>()
  const [picked, setPicked] = useState<string>()
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState('')
  const [connectionError, setConnectionError] = useState('')
  const [caseListError, setCaseListError] = useState('')
  const [status, setStatus] = useState('')
  const [query, setQuery] = useState('')
  const [cycle, setCycle] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const selected = cases.find((item) => item.case_id === caseId)
  const pickedCandidate = active?.candidates.find((item) => item.candidate_id === picked)

  useEffect(() => {
    if (!canRead) return
    const abort = new AbortController()
    setCaseListError('')
    client.cases(abort.signal).then(setCases).catch((error: unknown) => {
      if (!abort.signal.aborted) setCaseListError(operationError(error))
    })
    return () => abort.abort()
  }, [client, canRead, refresh])

  const acceptView = useCallback((next: CaseView) => {
    setView((prev) => {
      if (!prev || prev.case_id !== next.case_id) return next
      return next.revision >= prev.revision && next.route_revision >= prev.route_revision ? next : prev
    })
  }, [])

  useEffect(() => {
    if (!canRead || !caseId || screen !== 'monitoring') return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next = await client.view(caseId!, abort.signal)
        if (!abort.signal.aborted) {
          acceptView(next); setConnectionError(''); setCycle((value) => value + 1)
        }
      } catch (error) {
        if (!abort.signal.aborted) setConnectionError(operationError(error))
      } finally {
        if (!abort.signal.aborted) timer = setTimeout(poll, 5000)
      }
    }
    void poll()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [client, caseId, screen, canRead, refresh, acceptView])

  function openCase(id: string) {
    if (caseId !== id) setView(undefined)
    setCaseId(id); setConnectionError(''); setStatus(''); setScreen('monitoring')
  }

  function openDialog(next: typeof dialog) {
    setActionError('')
    setDialog(next)
  }

  async function decide(decision: Decision) {
    if (!active || !pickedCandidate || busy) return
    setBusy(true); setActionError('')
    try {
      acceptView(await client.decide(active.case_id, pickedCandidate, decision, user))
      setStatus('Decision submitted to Controls.')
      setRefresh((value) => value + 1)
      setPicked(undefined)
    } catch (error) {
      setActionError(operationError(error))
    } finally { setBusy(false) }
  }

  async function command(action: Command) {
    if (!active || busy) return
    setBusy(true); setActionError('')
    try {
      const next = await client.command(active.case_id, action, user)
      if (action === 'DELETE') {
        setCaseId(undefined); setView(undefined); setScreen('cases')
      } else {
        acceptView(next)
      }
      setRefresh((value) => value + 1)
      setStatus(commandLabels[action] + ' submitted.')
      setDialog(undefined)
    } catch (error) {
      setActionError(operationError(error))
    } finally { setBusy(false) }
  }

  const lifecycle = active?.lifecycle
  const retained = ['CLOSED', 'EXPORTING', 'SAVED'].includes(lifecycle ?? '')
  const confirmedClips = retained ? (active?.clips ?? []).filter((clip) => clip.state === 'CONFIRMED') : []
  const actions: Command[] = []
  if (allowed(user, 'case.close') && (lifecycle === 'PREPARING' || lifecycle === 'SEARCHING')) actions.push('CLOSE')
  if (allowed(user, 'case.export') && ['CLOSED', 'SAVED'].includes(lifecycle ?? '')) actions.push('EXPORT')
  if (allowed(user, 'case.delete') && ['CLOSED', 'SAVED', 'FAILED'].includes(lifecycle ?? '')) actions.push('DELETE')
  const visibleCases = cases
    .filter((item) => [item.case_id, item.target_id, item.site_id, item.lifecycle].some((text) => text.toLowerCase().includes(query.toLowerCase().trim())))
  const canReview = allowed(user, 'case.review') && ['SEARCHING', 'CLOSED'].includes(lifecycle ?? '')

  return <div className="app-shell trace-console">
    <a href="#main" className="skip">Skip to workspace</a>
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Aperture size={24}/></span><span><strong>TRACE</strong><small>OPERATIONS</small></span></div>
      <div className="workspace-id"><span className="site-mark"><ShieldCheck size={17}/></span><span><strong>{selected?.site_id ?? 'JAVES-AI'}</strong><small>Connected workspace</small></span><ChevronsUpDown size={14}/></div>
      <nav aria-label="Workspace navigation">
        <span className="nav-label">Workspace</span>
        {canRead && <>
          <button aria-current={screen === 'monitoring' ? 'page' : undefined} onClick={() => setScreen('monitoring')}><LayoutGrid size={18}/> Monitoring</button>
          <button aria-current={screen === 'cases' ? 'page' : undefined} onClick={() => setScreen('cases')}><FolderKanban size={18}/> Cases</button>
        </>}
        {canManageAccounts && <button aria-current={screen === 'accounts' ? 'page' : undefined} onClick={() => setScreen('accounts')}><Users size={18}/> Accounts</button>}
      </nav>
      <div className="sidebar-bottom">
        <button className="help" onClick={() => openDialog('help')}><CircleHelp size={17}/> Workspace help</button>
        <div className="profile"><span className="avatar">{user.username.slice(0, 2).toUpperCase()}</span><span><strong>{user.username}</strong><small>{label(user.role)}</small></span>
          <button className="icon-button" aria-label="Sign out" disabled={busy} onClick={signOut}><LogOut size={17}/></button>
        </div>
      </div>
    </aside>
    <main id="main" className="main">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><span>/</span><strong>{canRead || canManageAccounts ? screenLabels[screen] : 'Access'}</strong></div>
        <div className="topbar-actions">
          <span className="mode-pill connected"><i className="dot"/>Controls connected</span>
          {(canRead || canManageAccounts) && <button className="icon-button" aria-label="Refresh workspace" onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={17}/></button>}
        </div>
      </header>
      <div className="content">
        {screen === 'monitoring' && connectionError && <p className="notice error" role="alert">{connectionError}</p>}
        {screen !== 'accounts' && status && <p className="notice success" role="status"><Check size={16}/>{status}</p>}
        {screen === 'accounts' && canManageAccounts ? <Accounts client={client} canCreate={allowed(user, 'user.create')} refresh={refresh}/>
        : !canRead ? <div className="panel empty"><h1>Dashboard access required</h1><p className="muted">This account does not have permission to view cases.</p></div>
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
            <Workspace key={active.case_id} view={active} demo={false} client={client} cycle={cycle}/>
            <ReviewQueue key={active.case_id} items={active.candidates} onPick={(candidate) => { setPicked(candidate.candidate_id); setActionError('') }}/>
          </div>
          <CaseProgress view={active}/>
          {(confirmedClips.length > 0 || active.export?.status) && <section className="panel case-export" aria-labelledby="case-export-heading">
            <h2 id="case-export-heading">Case recordings</h2>
            {confirmedClips.map((clip) => {
              const title = [clip.camera_id ?? 'Confirmed clip', clip.t_start && time(clip.t_start) + ' KST'].filter(Boolean).join(' · ')
              return clip.status === 'READY' && clip.url
                ? <CaseVideo key={clip.clip_id} client={client} path={clip.url} title={title}/>
                : <p key={clip.clip_id}>{title} · {label(clip.status)}</p>
            })}
            {active.export?.status === 'READY' && active.export.url
              ? <CaseVideo key={active.export.export_request_id} client={client} path={active.export.url} title="Play merged clip" download={active.case_id + '.mp4'}/>
              : active.export?.status && <p className="muted">Merged clip · {label(active.export.status)}
                {active.export.error_code && ' · ' + label(active.export.error_code)}</p>}
          </section>}
        </> : <div className="panel empty"><Activity size={30}/><h1>{caseId ? 'Loading case' : 'Select a search case'}</h1><p className="muted">{caseId ? caseId : 'Choose a case from the workspace to begin monitoring.'}</p>
          <button className="secondary" onClick={() => setScreen('cases')}><FolderKanban size={16}/> Browse cases</button></div>}
      </div>
    </main>
    {dialog === 'create' && allowed(user, 'case.create') && <NewCase client={client} user={user}
      close={() => openDialog(undefined)} created={(created) => {
        openCase(created.case_id)
        acceptView(created)
        setDialog(undefined)
        setRefresh((value) => value + 1)
      }}/>}
    {dialog === 'help' && <Modal title="Workspace help" close={() => openDialog(undefined)}><p className="muted">Open a case to review sightings, its route and available recordings. Your JAVES-AI account controls which actions you can take.</p></Modal>}
    {dialog && dialog in commandLabels && <Modal title={commandLabels[dialog as Command]} close={() => openDialog(undefined)}>
      <p className="muted">Apply this action to {active?.case_id}?</p>{actionError && <p className="notice error" role="alert">{actionError}</p>}
      <div className="dialog-actions"><button className="secondary" onClick={() => openDialog(undefined)}>Cancel</button><button className="primary" disabled={busy} onClick={() => void command(dialog as Command)}>Confirm</button></div>
    </Modal>}
    {pickedCandidate && <CandidateDialog candidate={pickedCandidate} client={client}
      clip={active?.clips?.find((clip) => clip.clip_id === pickedCandidate.clip_id || clip.candidate_id === pickedCandidate.candidate_id)}
      close={() => setPicked(undefined)} submit={(decision) => void decide(decision)} canReview={canReview} busy={busy} error={actionError}/>}
  </div>
}
