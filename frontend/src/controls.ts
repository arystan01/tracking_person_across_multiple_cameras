export type UserRole = 'ROOT' | 'ADMIN' | 'OPERATOR' | 'VIEWER' | 'SERVICE'

export type User = {
  user_id: string
  username: string
  role: UserRole
  permissions: string[]
  must_change_password: boolean
}

export type Session = { access_token: string; user: User }
export type Account = Pick<User, 'user_id' | 'username' | 'role' | 'must_change_password'> & { active: boolean }
export type CaseSummary = {
  case_id: string
  target_id: string
  site_id: string
  site_version: string
  lifecycle: string
  mode?: string | null
  created_at: string
  updated_at: string
}

export type Candidate = {
  candidate_id: string
  tracklet_id: string
  camera_id?: string | null
  appearance_score?: number | null
  t_start?: string
  t_end?: string
  review_state?: string
  clip_id?: string | null
  thumbnail_refs?: string[]
}

export type RouteMap = { floor_id: string; url: string; alt_text: string }
export type Clip = {
  clip_id: string
  candidate_id?: string | null
  status: string
  state?: string | null
  camera_id?: string | null
  t_start?: string
  error_code?: string | null
  url?: string | null
}

type RouteVisit = { id?: string; visit_id?: string; camera?: string; camera_id?: string; t_in: string; source?: string }
type RouteHypothesis = { id?: string; visits: RouteVisit[] }

export type CaseView = {
  case_id: string
  revision: number
  route_revision: number
  lifecycle: string
  mode?: string | null
  done: boolean
  stop_reason?: string | null
  candidates: Candidate[]
  rendered_route_maps: RouteMap[]
  live?: { camera_id: string; observed_at?: string; stream_url?: string | null } | null
  watch: { camera_id: string }[]
  search_progress: { jobs_total?: number; jobs_by_state?: Record<string, number>; processed_camera_seconds?: number }
  errors: { message?: string; error?: string; code?: string; camera_id?: string }[]
  unobserved_windows: { camera_id?: string; t_from?: string; t_to?: string; reason?: string }[]
  best_hypothesis?: RouteHypothesis | null
  alternatives?: RouteHypothesis[]
  estimate?: { last_observed_camera?: string | null; last_observed_time?: string | null; valid_until?: string | null } | null
  route_presentation?: { shown_layers?: string[]; search_area_as_of?: string | null } | null
  clips?: Clip[]
  export?: { export_request_id?: string | null; status?: string; url?: string | null; error_code?: string | null } | null
}

type ReferenceImageRef = { object_key: string; archive_id?: string | null; uri?: string | null }

export type CaseInput = {
  case_id: string
  target_id: string
  site_id: string
  site_version: string
  reference_image_ref: ReferenceImageRef
  last_known: { camera_id: string; time: string }
}
export type Decision = 'CONFIRM' | 'REJECT'
export type Command = 'CLOSE' | 'EXPORT' | 'DELETE'

export class ControlsError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message)
  }
}

export const casePath = (id: string) => '/v1/cases/' + encodeURIComponent(id)
export const allowed = (user: User | undefined, permission: string) =>
  !!user && (user.permissions.includes('*') || user.permissions.includes(permission))

type RequestOptions = { body?: object | FormData; key?: string; signal?: AbortSignal; mime?: string; method?: 'POST' }

export class Controls {
  private readonly pending = new Map<string, { key: string; issuedAt: string }>()

  private readonly referenceUploads = new WeakMap<File, { key: string; reference?: CaseInput['reference_image_ref'] }>()

  constructor(private readonly token?: string, private readonly onExpired?: () => void) {}

  private async request(path: string, options: RequestOptions = {}) {
    const url = new URL(path, location.origin)
    if (!path.startsWith('/v1/') || url.origin !== location.origin || !url.pathname.startsWith('/v1/')) {
      throw new ControlsError('Controls returned an invalid resource path.')
    }
    const headers = new Headers({ Accept: options.mime || 'application/json' })
    if (this.token) headers.set('Authorization', 'Bearer ' + this.token)
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
    if (options.key) headers.set('Idempotency-Key', options.key)
    const timeout = AbortSignal.timeout(12000)
    const signal = options.signal ? AbortSignal.any([timeout, options.signal]) : timeout
    let response: Response
    try {
      response = await fetch(url, {
        method: options.method || (options.body ? 'POST' : 'GET'), headers, signal,
        redirect: 'error', cache: 'no-store',
        body: options.body instanceof FormData ? options.body : options.body ? JSON.stringify(options.body) : undefined,
      })
    } catch {
      if (options.signal?.aborted) throw options.signal.reason
      throw new ControlsError('Controls is offline or did not respond. Try again.')
    }
    if (!response.ok) {
      const problem = await response.json().catch(() => null)
      const detail = typeof problem?.detail === 'string' ? problem.detail : null
      if (response.status === 401) {
        // Controls also uses 401 when a valid session supplies the wrong current password.
        if (path === '/v1/auth/change-password' && detail === 'current password is incorrect') {
          throw new ControlsError('The current password is incorrect.', 401)
        }
        if (!this.token) throw new ControlsError('Sign-in failed. ' + (detail ?? 'Check your username and password.'), 401)
        this.onExpired?.()
        throw new ControlsError('Your session expired. Sign in to continue.', 401)
      }
      if (response.status === 403) {
        throw new ControlsError('Access denied. ' + (detail ?? 'Your account does not have permission for this action.'), 403)
      }
      throw new ControlsError(detail ?? 'Controls could not complete the request (' + response.status + ').', response.status)
    }
    return response
  }

  private async json<T>(path: string, options?: RequestOptions): Promise<T> {
    const response = await this.request(path, options)
    if (response.status === 204) return undefined as T
    try {
      return await response.json() as T
    } catch {
      throw new ControlsError('Controls returned an invalid response.')
    }
  }

  login(username: string, password: string) {
    return this.json<Session>('/v1/auth/login', { body: { username, password } })
  }

  me() {
    return this.json<User>('/v1/auth/me')
  }

  logout() {
    return this.json<void>('/v1/auth/logout', { method: 'POST' })
  }

  changePassword(current_password: string, new_password: string) {
    return this.json<void>('/v1/auth/change-password', {
      body: { current_password, new_password }, key: crypto.randomUUID(),
    })
  }

  accounts(signal?: AbortSignal) {
    return this.json<Account[]>('/v1/admin/users', { signal })
  }

  createAccount(username: string, temporary_password: string, role: UserRole) {
    return this.json<Account>('/v1/admin/users', {
      body: { username, temporary_password, role }, key: crypto.randomUUID(),
    })
  }

  cases(signal?: AbortSignal) {
    return this.json<CaseSummary[]>('/v1/cases', { signal })
  }

  view(id: string, signal?: AbortSignal) {
    return this.json<CaseView>(casePath(id) + '/view', { signal })
  }

  async media(path: string, mime: string, signal: AbortSignal) {
    const response = await this.request(path, { signal, mime })
    const contentType = response.headers.get('content-type')?.split(';')[0].trim()
    const matches = mime.endsWith('/*') ? contentType?.startsWith(mime.slice(0, -1)) : contentType === mime
    if (!matches) {
      throw new ControlsError('Controls returned an unexpected media type.')
    }
    return await response.blob()
  }

  // Preserve the payload and its key when the server may already have committed a timed-out request.
  private async mutate(path: string, fields: object, makeBody: (key: string, issuedAt: string) => object) {
    const signature = JSON.stringify([path, fields])
    const entry = this.pending.get(signature) ?? { key: crypto.randomUUID(), issuedAt: new Date().toISOString() }
    this.pending.set(signature, entry)
    try {
      const view = await this.json<CaseView>(path, { body: makeBody(entry.key, entry.issuedAt), key: entry.key })
      this.pending.delete(signature)
      return view
    } catch (error) {
      if (error instanceof ControlsError && error.status && error.status < 500) this.pending.delete(signature)
      throw error
    }
  }

  async stageReference(caseId: string, file: File) {
    const upload = this.referenceUploads.get(file) ?? { key: crypto.randomUUID() }
    if (upload.reference) return upload.reference
    this.referenceUploads.set(file, upload)
    const body = new FormData()
    body.set('file', file)
    let response: { reference_image_ref: ReferenceImageRef }
    try {
      response = await this.json(casePath(caseId) + '/reference-image', { body, key: upload.key })
    } catch (error) {
      if (error instanceof ControlsError && [404, 405, 501].includes(error.status ?? 0)) {
        throw new ControlsError('Reference photo upload is unavailable on this Controls server.')
      }
      throw error
    }
    const reference = response?.reference_image_ref
    const key = reference?.object_key
    const prefix = 'cases/' + caseId + '/'
    if (typeof key !== 'string' || !key.startsWith(prefix) ||
        key.slice(prefix.length).split('/').some((part) => !part || part === '.' || part === '..') || key.includes('\\')) {
      throw new ControlsError('Controls returned an invalid reference photo key.')
    }
    upload.reference = reference
    return upload.reference
  }

  create(input: CaseInput, user: User) {
    return this.mutate('/v1/cases', input, (_, issuedAt) => ({
      ...input, operator_id: user.user_id, window: { start: input.last_known.time, end: issuedAt },
    }))
  }

  decide(id: string, candidate: Candidate, decision: Decision, user: User) {
    const fields = { candidate_id: candidate.candidate_id, tracklet_id: candidate.tracklet_id, decision, operator_id: user.user_id }
    return this.mutate(casePath(id) + '/decisions', fields, (decision_id, occurred_at) => ({
      ...fields, decision_id, occurred_at,
    }))
  }

  command(id: string, action: Command, user: User) {
    const fields = { action, operator_id: user.user_id }
    return this.mutate(casePath(id) + '/commands/' + action.toLowerCase(), fields, (command_id, occurred_at) => ({
      ...fields, command_id, occurred_at,
    }))
  }
}
