import { useEffect, useState, type FormEvent } from 'react'
import { KeyRound, Plus, Power, UserRound } from 'lucide-react'
import { type Account, type Controls, type UserRole } from './controls'
import { label, Modal } from './Workspace'

const humanRoles = ['VIEWER', 'OPERATOR', 'ADMIN', 'ROOT'] as const satisfies readonly UserRole[]
const errorMessage = (error: unknown) => error instanceof Error ? error.message : 'Try again.'

function CreateAccount({ client, close, created }: { client: Controls; close: () => void; created: (account: Account) => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const form = event.currentTarget
    const fields = new FormData(form)
    const password = form.elements.namedItem('temporary_password') as HTMLInputElement
    password.value = ''
    setBusy(true)
    setError('')
    try {
      const account = await client.createAccount(String(fields.get('username')).trim(),
        String(fields.get('temporary_password')), fields.get('role') as UserRole)
      created(account)
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setBusy(false)
    }
  }

  return <Modal title="New account" close={() => { if (!busy) close() }}>
    <form className="form" aria-busy={busy} onSubmit={submit}>
      <label>Username<input name="username" autoCapitalize="none" minLength={3} maxLength={128} required/></label>
      <label>Role<select name="role" defaultValue="VIEWER">
        {humanRoles.map((role) => <option value={role} key={role}>{label(role)}</option>)}
      </select></label>
      <label>Temporary password<input name="temporary_password" type="password" autoComplete="new-password"
        minLength={12} maxLength={1024} required/></label>
      <p className="muted">The user will change this password at first sign-in.</p>
      {error && <p className="notice error" role="alert">{error}</p>}
      <button type="submit" className="primary" disabled={busy}>{busy ? 'Creating…' : 'Create account'}</button>
    </form>
  </Modal>
}

export function Accounts({ client, permissions, refresh }: { client: Controls; permissions: string[]; refresh: number }) {
  const [accounts, setAccounts] = useState<Account[]>()
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [creating, setCreating] = useState(false)
  const [reload, setReload] = useState(0)
  const [busyId, setBusyId] = useState('')
  const permits = (permission: string) => permissions.includes('*') || permissions.includes(permission)
  const canCreate = permits('user.create')

  async function update(account: Account, action: 'role' | 'active' | 'password', value?: string) {
    if (busyId) return
    setBusyId(account.user_id); setError(''); setNotice('')
    try {
      if (action === 'role') await client.setRole(account.user_id, value as UserRole)
      else if (action === 'active') await client.setActive(account.user_id, !account.active)
      else await client.resetPassword(account.user_id, value ?? '')
      setNotice('Account updated for ' + account.username + '.')
      setReload((current) => current + 1)
    } catch (failure) { setError(errorMessage(failure)) }
    finally { setBusyId('') }
  }

  useEffect(() => {
    const abort = new AbortController()
    setError('')
    client.accounts(abort.signal).then((result) => {
      if (!abort.signal.aborted) setAccounts(result)
    }).catch((failure: unknown) => {
      if (!abort.signal.aborted) {
        setAccounts(undefined)
        setError(errorMessage(failure))
      }
    })
    return () => abort.abort()
  }, [client, refresh, reload])

  return <>
    <div className="page-head">
      <div><h1>Accounts</h1><p>JAVES-AI users and their assigned roles.</p></div>
      {canCreate && <button className="primary" onClick={() => { setNotice(''); setCreating(true) }}><Plus size={16}/> New account</button>}
    </div>
    <section className="panel cases-panel" aria-label="User accounts">
      {error && <p className="notice error" role="alert">{error}</p>}
      {notice && <p className="notice success" role="status">{notice}</p>}
      {!error && !accounts && <p className="muted" role="status">Loading accounts…</p>}
      {accounts?.length === 0 && <p className="muted">No accounts to show.</p>}
      {accounts?.map((account) => <div className="case-row account-row" key={account.user_id}>
        <UserRound size={20}/><span><strong>{account.username}</strong><small>{label(account.role)}</small></span>
        <span className="tag">{!account.active ? 'Disabled' : account.must_change_password ? 'Password change required' : 'Active'}</span>
        <div className="account-actions">
          {permits('user.role') && <select aria-label={'Role for ' + account.username} value={account.role} disabled={!!busyId}
            onChange={(event) => void update(account, 'role', event.target.value)}>{humanRoles.map((role) => <option value={role} key={role}>{label(role)}</option>)}</select>}
          {permits(account.active ? 'user.disable' : 'user.enable') && <button className="secondary" disabled={!!busyId}
            onClick={() => void update(account, 'active')}><Power size={14}/>{account.active ? 'Disable' : 'Enable'}</button>}
          {permits('user.reset_password') && <form onSubmit={(event) => {
            event.preventDefault(); const form = event.currentTarget
            const password = String(new FormData(form).get('temporary_password') ?? '')
            void update(account, 'password', password); form.reset()
          }}><input name="temporary_password" type="password" minLength={12} maxLength={1024} required
              aria-label={'Temporary password for ' + account.username} placeholder="Temporary password"/>
            <button className="secondary" disabled={!!busyId}><KeyRound size={14}/> Reset</button></form>}
        </div>
      </div>)}
    </section>
    {canCreate && creating && <CreateAccount client={client} close={() => setCreating(false)} created={(account) => {
      setCreating(false)
      setNotice('Account created for ' + account.username + '.')
      setReload((value) => value + 1)
    }}/>}
  </>
}
