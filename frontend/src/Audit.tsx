import { useEffect, useState } from 'react'
import { Download, FileClock } from 'lucide-react'
import { type AuditExport, type Controls } from './controls'

export function Audit({ client, refresh }: { client: Controls; refresh: number }) {
  const [events, setEvents] = useState<Record<string, unknown>[]>([])
  const [manifest, setManifest] = useState<AuditExport>()
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const abort = new AbortController()
    client.iamAudit(abort.signal).then(setEvents).catch((failure: unknown) => {
      if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : 'Audit is unavailable.')
    })
    return () => abort.abort()
  }, [client, refresh])
  return <>
    <div className="page-head"><div><h1>Security audit</h1><p>Permanent IAM audit and signed case audit exports.</p></div>
      <button className="primary" disabled={busy} onClick={async () => {
        setBusy(true); setError('')
        try { setManifest(await client.createAuditExport()) }
        catch (failure) { setError(failure instanceof Error ? failure.message : 'Export failed.') }
        finally { setBusy(false) }
      }}><FileClock size={16}/>{busy ? 'Preparing…' : 'Create audit export'}</button></div>
    {error && <p className="notice error" role="alert">{error}</p>}
    {manifest && <section className="panel"><h2>Audit export ready</h2><p>{manifest.row_count} events · SHA-256 {manifest.sha256}</p>
      <div className="dialog-actions"><a className="secondary" href={'/dashboard-api/controls' + manifest.events_url}><Download size={15}/> Events</a><a className="secondary" href={'/dashboard-api/controls' + manifest.manifest_url}><Download size={15}/> Manifest</a></div></section>}
    <section className="panel cases-panel"><h2>IAM events</h2>{events.length ? events.map((event, index) => <div className="case-row" key={String(event.event_id ?? index)}><FileClock size={18}/><span><strong>{String(event.event_type ?? 'Event')}</strong><small>{String(event.occurred_at ?? '')}</small></span></div>) : <p className="muted">No IAM events.</p>}</section>
  </>
}
