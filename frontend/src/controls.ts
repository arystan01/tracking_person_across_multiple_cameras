import { z } from 'zod'

export type UserRole = 'ROOT' | 'ADMIN' | 'OPERATOR' | 'VIEWER' | 'SERVICE'

const userSchema = z.object({
  user_id: z.string(), username: z.string(),
  role: z.enum(['ROOT', 'ADMIN', 'OPERATOR', 'VIEWER', 'SERVICE']),
  permissions: z.array(z.string()), must_change_password: z.boolean(),
}).passthrough()

export type User = z.infer<typeof userSchema>
export type Session = { user: User; expires_at: string; csrf_token: string }
export type Account = Pick<User, 'user_id' | 'username' | 'role' | 'must_change_password'> & { active: boolean }
export type SiteCatalog = {
  site_id: string; site_version: string; display_name: string; timezone: string
  cameras: { camera_id: string; display_name: string; floor_id?: string | null; zone_id?: string | null }[]
  zones: string[]
}
export type CaseSummary = {
  case_id: string; target_id: string; site_id: string; site_version: string
  lifecycle: string; mode?: string | null; created_at: string; updated_at: string
  revision: number; route_revision: number
}
export type CaseDetail = CaseSummary & {
  profile: 'ADULT' | 'CHILD' | 'ELDERLY'; params_version: string
  last_known: { camera_id?: string | null; zone_id?: string | null; time: string }
  window: { start: string; end: string }; reference_preview_url?: string | null
  closed_at?: string | null; retention_due_at?: string | null
}
export type Candidate = {
  candidate_id: string; tracklet_id: string; camera_id?: string | null
  appearance_score?: number | null; calibration_version?: string | null
  t_start?: string | null; t_end?: string | null
  decision_state: 'UNREVIEWED' | 'CONFIRMED' | 'REJECTED'
  active_decision_id?: string | null; requires_confirmation?: boolean; flags?: string[]
  clip_id?: string | null; clip_status?: string | null; clip_state?: string | null
  thumbnail_refs?: string[]
}
export type RouteMap = { floor_id: string; url: string; alt_text: string; width?: number; height?: number }
export type Clip = {
  clip_id: string; candidate_id?: string | null; status: string; state?: string | null
  camera_id?: string | null; t_start?: string; error_code?: string | null; url?: string | null
}
type RouteVisit = { id?: string; visit_id?: string; camera?: string; camera_id?: string; t_in: string; source?: string }
type RouteHypothesis = { id?: string; visits: RouteVisit[] }
export type CaseView = {
  case_id: string; revision: number; route_revision: number; lifecycle: string
  mode?: string | null; done: boolean; stop_reason?: string | null
  candidates: Candidate[]; rendered_route_maps: RouteMap[]
  live?: { camera_id: string; observed_at?: string; stream_url?: string | null; expires_at?: string | null } | null
  watch: { camera_id: string }[]
  search_progress: { jobs_total?: number; jobs_by_state?: Record<string, number>; processed_camera_seconds?: number }
  errors: { message?: string; error?: string; code?: string; camera_id?: string }[]
  unobserved_windows: { camera_id?: string; t_from?: string; t_to?: string; reason?: string }[]
  best_hypothesis?: RouteHypothesis | null; alternatives?: RouteHypothesis[]
  estimate?: { last_observed_camera?: string | null; last_observed_time?: string | null; valid_until?: string | null } | null
  route_presentation?: { shown_layers?: string[]; search_area_as_of?: string | null } | null
  clips?: Clip[]
  export?: { export_request_id?: string | null; status?: string; url?: string | null; error_code?: string | null } | null
}
export type CaseInput = {
  case_id: string; target_id: string; site_id: string; site_version: string
  reference_upload_id: string
  last_known: { camera_id?: string; zone_id?: string; time: string }
  window: { start: string; end: string }
  profile: 'ADULT' | 'CHILD' | 'ELDERLY'
}
export type Decision = 'CONFIRM' | 'REJECT'
export type Command = 'CLOSE' | 'EXPORT' | 'DELETE'
export type DecisionRecord = {
  decision_id: string; candidate_id: string; tracklet_id: string; decision: Decision
  occurred_at: string; active: boolean; resulting_route_revision?: number | null
}
export type LegalHold = {
  hold_id: string; external_ticket_id: string; reason: string; active: boolean
  set_at: string; released_at?: string | null
}

const candidateSchema = z.object({
  candidate_id: z.string(), tracklet_id: z.string(), camera_id: z.string().nullish(),
  appearance_score: z.number().nullish(), calibration_version: z.string().nullish(),
  t_start: z.string().nullish(), t_end: z.string().nullish(),
  decision_state: z.enum(['UNREVIEWED', 'CONFIRMED', 'REJECTED']).default('UNREVIEWED'),
  active_decision_id: z.string().nullish(), requires_confirmation: z.boolean().optional(),
  flags: z.array(z.string()).optional(), clip_id: z.string().nullish(),
  clip_status: z.string().nullish(), clip_state: z.string().nullish(),
  thumbnail_refs: z.array(z.string()).optional(),
}).passthrough()
const caseViewSchema = z.object({
  case_id: z.string(), revision: z.number(), route_revision: z.number(), lifecycle: z.string(),
  mode: z.string().nullish(), done: z.boolean().default(false), stop_reason: z.string().nullish(),
  candidates: z.array(candidateSchema).default([]), rendered_route_maps: z.array(z.object({
    floor_id: z.string(), url: z.string(), alt_text: z.string(), width: z.number().optional(), height: z.number().optional(),
  }).passthrough()).default([]),
  live: z.object({ camera_id: z.string(), observed_at: z.string().optional(), stream_url: z.string().nullish(), expires_at: z.string().nullish() }).passthrough().nullish(),
  watch: z.array(z.object({ camera_id: z.string() }).passthrough()).default([]),
  search_progress: z.object({ jobs_total: z.number().optional(), jobs_by_state: z.record(z.string(), z.number()).optional(), processed_camera_seconds: z.number().optional() }).passthrough().default({}),
  errors: z.array(z.object({ message: z.string().optional(), error: z.string().optional(), code: z.string().optional(), camera_id: z.string().optional() }).passthrough()).default([]),
  unobserved_windows: z.array(z.object({ camera_id: z.string().optional(), t_from: z.string().optional(), t_to: z.string().optional(), reason: z.string().optional() }).passthrough()).default([]),
  best_hypothesis: z.any().nullish(), alternatives: z.array(z.any()).optional(), estimate: z.any().nullish(),
  route_presentation: z.any().nullish(), clips: z.array(z.any()).optional(), export: z.any().nullish(),
}).passthrough()

export class ControlsError extends Error {
  constructor(message: string, readonly status?: number, readonly code?: string, readonly retryable = false) { super(message) }
}
export const casePath = (id: string) => '/v1/cases/' + encodeURIComponent(id)
export const allowed = (user: User | undefined, permission: string) =>
  !!user && (user.permissions.includes('*') || user.permissions.includes(permission))
type RequestOptions = { body?: object | FormData; key?: string; signal?: AbortSignal; mime?: string; method?: 'POST' | 'PUT' }
const controlsPath = (path: string) => '/dashboard-api/controls' + path

export class Controls {
  private readonly pending = new Map<string, { key: string; issuedAt: string }>()
  private readonly referenceUploads = new WeakMap<File, { key: string; uploadId?: string }>()
  constructor(private readonly csrfToken?: string, private readonly onExpired?: () => void) {}

  private async request(path: string, options: RequestOptions = {}) {
    const directSession = path.startsWith('/dashboard-api/session/')
    const target = directSession ? path : controlsPath(path)
    const url = new URL(target, location.origin)
    if (url.origin !== location.origin || (!directSession && !path.startsWith('/v1/'))) throw new ControlsError('Dashboard returned an invalid resource path.')
    const headers = new Headers({ Accept: options.mime || 'application/json' })
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
    if (options.key) headers.set('Idempotency-Key', options.key)
    const method = options.method || (options.body ? 'POST' : 'GET')
    if (method !== 'GET' && this.csrfToken) headers.set('X-JAVES-CSRF', this.csrfToken)
    const timeout = AbortSignal.timeout(35_000)
    const signal = options.signal ? AbortSignal.any([timeout, options.signal]) : timeout
    let response: Response
    try {
      response = await fetch(url, { method, headers, signal, credentials: 'same-origin', redirect: 'error', cache: 'no-store',
        body: options.body instanceof FormData ? options.body : options.body ? JSON.stringify(options.body) : undefined })
    } catch {
      if (options.signal?.aborted) throw options.signal.reason
      throw new ControlsError('Dashboard is offline or did not respond. Try again.', undefined, 'OFFLINE', true)
    }
    if (!response.ok) {
      const problem = await response.json().catch(() => null)
      const detail = typeof problem?.detail === 'string' ? problem.detail : null
      if (response.status === 401 && !(directSession && path.endsWith('/login'))) this.onExpired?.()
      throw new ControlsError(detail ?? 'The request could not be completed (' + response.status + ').', response.status,
        typeof problem?.code === 'string' ? problem.code : undefined, Boolean(problem?.retryable))
    }
    return response
  }
  private async json<T>(path: string, options?: RequestOptions, schema?: z.ZodType<T>): Promise<T> {
    const response = await this.request(path, options)
    if (response.status === 204) return undefined as T
    const payload = await response.json().catch(() => { throw new ControlsError('Dashboard returned invalid JSON.') })
    if (!schema) return payload as T
    const parsed = schema.safeParse(payload)
    if (!parsed.success) throw new ControlsError('Dashboard returned an incompatible response.')
    return parsed.data
  }
  login(username: string, password: string) { return this.json<Session>('/dashboard-api/session/login', { body: { username, password } }, z.object({ user: userSchema, expires_at: z.string(), csrf_token: z.string() })) }
  session() { return this.json<Session>('/dashboard-api/session/me', undefined, z.object({ user: userSchema, expires_at: z.string(), csrf_token: z.string() })) }
  logout() { return this.json<void>('/dashboard-api/session/logout', { method: 'POST' }) }
  changePassword(current_password: string, new_password: string) { return this.json<void>('/dashboard-api/session/change-password', { body: { current_password, new_password }, key: crypto.randomUUID() }) }
  sites(signal?: AbortSignal) { return this.json<SiteCatalog[]>('/v1/sites', { signal }) }
  accounts(signal?: AbortSignal) { return this.json<Account[]>('/v1/admin/users', { signal }) }
  createAccount(username: string, temporary_password: string, role: UserRole) { return this.json<Account>('/v1/admin/users', { body: { username, temporary_password, role }, key: crypto.randomUUID() }) }
  setRole(userId: string, role: UserRole) { return this.json<Account>('/v1/admin/users/' + encodeURIComponent(userId) + '/role', { method: 'PUT', body: { role }, key: crypto.randomUUID() }) }
  setActive(userId: string, active: boolean) { return this.json<Account>('/v1/admin/users/' + encodeURIComponent(userId) + '/' + (active ? 'enable' : 'disable'), { method: 'POST', key: crypto.randomUUID() }) }
  resetPassword(userId: string, temporary_password: string) { return this.json<Account>('/v1/admin/users/' + encodeURIComponent(userId) + '/reset-password', { body: { temporary_password }, key: crypto.randomUUID() }) }
  cases(signal?: AbortSignal) { return this.json<CaseSummary[]>('/v1/cases', { signal }) }
  detail(id: string, signal?: AbortSignal) { return this.json<CaseDetail>(casePath(id), { signal }) }
  view(id: string, signal?: AbortSignal) { return this.json<CaseView>(casePath(id) + '/view', { signal }, caseViewSchema) }
  events(id: string, signal?: AbortSignal) { return this.json<Record<string, unknown>[]>(casePath(id) + '/events', { signal }) }
  decisions(id: string, signal?: AbortSignal) { return this.json<DecisionRecord[]>(casePath(id) + '/decisions', { signal }) }
  legalHolds(id: string, signal?: AbortSignal) { return this.json<LegalHold[]>(casePath(id) + '/legal-holds', { signal }) }
  async media(path: string, mime: string, signal: AbortSignal) {
    const response = await this.request(path, { signal, mime })
    const contentType = response.headers.get('content-type')?.split(';')[0].trim()
    const matches = mime.endsWith('/*') ? contentType?.startsWith(mime.slice(0, -1)) : contentType === mime
    if (!matches) throw new ControlsError('Dashboard returned an unexpected media type.')
    return await response.blob()
  }
  private async mutate<T>(path: string, fields: object, makeBody: (key: string, issuedAt: string) => object, schema?: z.ZodType<T>) {
    const signature = JSON.stringify([path, fields])
    const entry = this.pending.get(signature) ?? { key: crypto.randomUUID(), issuedAt: new Date().toISOString() }
    this.pending.set(signature, entry)
    try {
      const result = await this.json<T>(path, { body: makeBody(entry.key, entry.issuedAt), key: entry.key }, schema)
      this.pending.delete(signature); return result
    } catch (error) {
      if (error instanceof ControlsError && !error.retryable) this.pending.delete(signature)
      throw error
    }
  }
  async stageReference(caseId: string, file: File) {
    const upload = this.referenceUploads.get(file) ?? { key: crypto.randomUUID() }
    if (upload.uploadId) return upload.uploadId
    this.referenceUploads.set(file, upload)
    const body = new FormData(); body.set('file', file)
    const response = await this.json<{ upload_id: string }>(casePath(caseId) + '/reference-image', { body, key: upload.key })
    if (!response.upload_id) throw new ControlsError('Dashboard did not return a reference upload ID.')
    upload.uploadId = response.upload_id; return upload.uploadId
  }
  create(input: CaseInput) { return this.mutate('/v1/cases', input, () => input, caseViewSchema) }
  decide(id: string, candidate: Candidate, decision: Decision) {
    const fields = { candidate_id: candidate.candidate_id, tracklet_id: candidate.tracklet_id, decision }
    return this.mutate(casePath(id) + '/decisions', fields, (decision_id, occurred_at) => ({ ...fields, decision_id, occurred_at }), caseViewSchema)
  }
  revoke(id: string, record: DecisionRecord, reason: string, expected_case_revision: number) {
    const fields = { decision_id: record.decision_id, reason, expected_case_revision }
    return this.mutate(casePath(id) + '/decisions/' + encodeURIComponent(record.decision_id) + '/revoke', fields, (revocation_id, occurred_at) => ({ ...fields, revocation_id, occurred_at }), caseViewSchema)
  }
  command(id: string, action: Command) {
    const fields = { action }
    return this.mutate(casePath(id) + '/commands/' + action.toLowerCase(), fields, (command_id, occurred_at) => ({ ...fields, command_id, occurred_at }), caseViewSchema)
  }
  setLegalHold(id: string, external_ticket_id: string, reason: string, expected_case_revision: number) {
    const fields = { external_ticket_id, reason, expected_case_revision }
    return this.mutate<LegalHold>(casePath(id) + '/legal-holds', fields, (hold_id, occurred_at) => ({ ...fields, hold_id, occurred_at }))
  }
  releaseLegalHold(id: string, hold: LegalHold, external_ticket_id: string, reason: string, expected_case_revision: number) {
    const fields = { hold_id: hold.hold_id, external_ticket_id, reason, expected_case_revision }
    return this.mutate<LegalHold>(casePath(id) + '/legal-holds/' + encodeURIComponent(hold.hold_id) + '/release', fields, (release_id, occurred_at) => ({ ...fields, release_id, occurred_at }))
  }
}
