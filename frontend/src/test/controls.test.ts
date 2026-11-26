import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { Controls } from '../controls'

const caseView = {
  case_id: 'case-1', revision: 1, route_revision: 0, lifecycle: 'SEARCHING', done: false,
  candidates: [], rendered_route_maps: [], search_progress: {}, watch: [],
  unobserved_windows: [], errors: [],
}
let decisionRequests: { key: string | null; body: Record<string, unknown> }[] = []
let failFirst = false

const server = setupServer(
  http.post('*/dashboard-api/session/login', async ({ request }) => {
    expect(request.headers.get('authorization')).toBeNull()
    return HttpResponse.json({
      user: { user_id: 'operator-1', username: 'operator', role: 'OPERATOR', permissions: ['case.review'], must_change_password: false },
      expires_at: '2030-01-01T00:00:00Z', csrf_token: 'csrf-1',
    })
  }),
  http.post('*/dashboard-api/controls/v1/cases/case-1/decisions', async ({ request }) => {
    decisionRequests.push({ key: request.headers.get('idempotency-key'), body: await request.json() as Record<string, unknown> })
    if (failFirst) { failFirst = false; return HttpResponse.json({ detail: 'retry', retryable: true }, { status: 503 }) }
    return HttpResponse.json(caseView)
  }),
)

beforeAll(() => server.listen())
afterEach(() => { server.resetHandlers(); decisionRequests = []; failFirst = false })
afterAll(() => server.close())

describe('Controls browser client', () => {
  it('keeps bearer authentication out of browser requests', async () => {
    const session = await new Controls().login('operator', 'secret')
    expect(session.csrf_token).toBe('csrf-1')
    expect(JSON.stringify(session)).not.toContain('access_token')
  })

  it('retries a mutation with the same idempotency identity', async () => {
    failFirst = true
    const client = new Controls('csrf-1')
    const candidate = { candidate_id: 'candidate-1', tracklet_id: 'tracklet-1', decision_state: 'UNREVIEWED' as const }
    await expect(client.decide('case-1', candidate, 'CONFIRM')).rejects.toMatchObject({ status: 503 })
    await client.decide('case-1', candidate, 'CONFIRM')
    expect(decisionRequests).toHaveLength(2)
    expect(decisionRequests[0].key).toBe(decisionRequests[1].key)
    expect(decisionRequests[0].body.decision_id).toBe(decisionRequests[1].body.decision_id)
    expect(decisionRequests[0].body.occurred_at).toBe(decisionRequests[1].body.occurred_at)
  })
})
