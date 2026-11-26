import { useEffect, useState, type FormEvent } from 'react'
import { Gavel, History, LockKeyhole, UnlockKeyhole } from 'lucide-react'
import { type CaseView, type Controls, type DecisionRecord, type LegalHold } from './controls'
import { label, time } from './Workspace'

const message = (error: unknown) => error instanceof Error ? error.message : 'The action could not be completed.'

export function CaseGovernance({ client, view, canReview, canLegalHold, onView, refresh }: {
  client: Controls; view: CaseView; canReview: boolean; canLegalHold: boolean
  onView: (view: CaseView) => void; refresh: () => void
}) {
  const [decisions, setDecisions] = useState<DecisionRecord[]>([])
  const [events, setEvents] = useState<Record<string, unknown>[]>([])
  const [holds, setHolds] = useState<LegalHold[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [cycle, setCycle] = useState(0)

  useEffect(() => {
    const abort = new AbortController()
    Promise.all([
      canReview ? client.decisions(view.case_id, abort.signal) : Promise.resolve([]),
      client.events(view.case_id, abort.signal),
      canLegalHold ? client.legalHolds(view.case_id, abort.signal) : Promise.resolve([]),
    ]).then(([nextDecisions, nextEvents, nextHolds]) => {
      if (!abort.signal.aborted) {
        setDecisions(nextDecisions); setEvents(nextEvents); setHolds(nextHolds)
      }
    }).catch((failure: unknown) => { if (!abort.signal.aborted) setError(message(failure)) })
    return () => abort.abort()
  }, [client, view.case_id, view.revision, canReview, canLegalHold, cycle])

  async function revoke(record: DecisionRecord, form: HTMLFormElement) {
    const reason = String(new FormData(form).get('reason') ?? '').trim()
    if (!reason || busy) return
    setBusy(true); setError('')
    try {
      onView(await client.revoke(view.case_id, record, reason, view.revision))
      form.reset(); setCycle((value) => value + 1); refresh()
    } catch (failure) { setError(message(failure)) }
    finally { setBusy(false) }
  }

  async function setHold(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (busy) return
    const form = event.currentTarget; const fields = new FormData(form)
    setBusy(true); setError('')
    try {
      await client.setLegalHold(view.case_id, String(fields.get('ticket')).trim(), String(fields.get('reason')).trim(), view.revision)
      form.reset(); setCycle((value) => value + 1); refresh()
    } catch (failure) { setError(message(failure)) }
    finally { setBusy(false) }
  }

  async function releaseHold(hold: LegalHold) {
    const ticket = window.prompt('External release ticket ID')?.trim()
    const reason = window.prompt('Reason for releasing this legal hold')?.trim()
    if (!ticket || !reason || busy) return
    setBusy(true); setError('')
    try {
      await client.releaseLegalHold(view.case_id, hold, ticket, reason, view.revision)
      setCycle((value) => value + 1); refresh()
    } catch (failure) { setError(message(failure)) }
    finally { setBusy(false) }
  }

  return <section className="panel governance" aria-labelledby="governance-title">
    <header className="panel-head"><div><h2 id="governance-title">Review history and governance</h2><p>Immutable operator decisions, case events and preservation controls.</p></div><Gavel size={18}/></header>
    {error && <p className="notice error" role="alert">{error}</p>}
    {canReview && <details open><summary><History size={15}/> Decision history ({decisions.length})</summary>
      {decisions.length === 0 ? <p className="muted">No operator decisions recorded.</p> : <ul className="governance-list">{decisions.map((record) => <li key={record.decision_id}>
        <span><strong>{label(record.decision)}</strong> · {record.candidate_id}<small>{time(record.occurred_at)} KST · {record.active ? 'Active' : 'Revoked'}</small></span>
        {record.active && <form onSubmit={(event) => { event.preventDefault(); void revoke(record, event.currentTarget) }}>
          <input name="reason" required maxLength={1000} placeholder="Reason for revoke" aria-label={'Revoke reason for ' + record.decision_id}/>
          <button className="secondary" disabled={busy}>Revoke</button>
        </form>}
      </li>)}</ul>}
    </details>}
    {canLegalHold && <details><summary><LockKeyhole size={15}/> Legal holds ({holds.filter((item) => item.active).length} active)</summary>
      <form className="hold-form" onSubmit={setHold}>
        <input name="ticket" required maxLength={256} placeholder="External ticket ID"/>
        <input name="reason" required maxLength={1000} placeholder="Preservation reason"/>
        <button className="secondary" disabled={busy}><LockKeyhole size={14}/> Set legal hold</button>
      </form>
      <ul className="governance-list">{holds.map((hold) => <li key={hold.hold_id}><span><strong>{hold.external_ticket_id}</strong><small>{hold.reason} · {hold.active ? 'Active' : 'Released'}</small></span>
        {hold.active && <button className="secondary" disabled={busy} onClick={() => void releaseHold(hold)}><UnlockKeyhole size={14}/> Release</button>}</li>)}</ul>
    </details>}
    <details><summary><History size={15}/> Case events ({events.length})</summary>
      <ol className="event-list">{events.slice().reverse().map((event, index) => <li key={String(event.event_id ?? index)}><strong>{label(String(event.event_type ?? 'event'))}</strong><span>{time(String(event.occurred_at ?? ''))} KST</span></li>)}</ol>
    </details>
  </section>
}
