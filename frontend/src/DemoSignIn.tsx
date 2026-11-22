import { useState } from 'react'
import { Aperture, ArrowRight, Camera, Eye, EyeOff, KeyRound, Layers, LockKeyhole, UserRound } from 'lucide-react'

type DemoSignInProps = {
  signIn: (username: string, password: string) => void
  busy: boolean
  error: string
}

export function DemoSignIn({ signIn, busy, error }: DemoSignInProps) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)

  return <main className="trace-console demo-signin">
    <div className="demo-signin-layout">
      <section className="demo-signin-access" aria-labelledby="sign-in-title">
        <div className="brand">
          <span className="brand-mark"><Aperture size={23} aria-hidden="true"/></span>
          <span><strong>TRACE</strong><small>OPERATIONS</small></span>
        </div>
        <div className="demo-signin-content">
          <header>
            <h1 id="sign-in-title">Operator sign-in</h1>
            <p>Open your case workspace.</p>
          </header>
          <form className="form demo-signin-form" aria-busy={busy} onSubmit={(event) => {
            event.preventDefault()
            if (busy) return
            const fields = new FormData(event.currentTarget)
            setShowPassword(false)
            signIn(String(fields.get('username')), String(fields.get('password')))
          }}>
            <label htmlFor="operator-username">Username</label>
            <div className="demo-signin-field">
              <UserRound size={17} aria-hidden="true"/>
              <input id="operator-username" name="username" autoComplete="username" autoCapitalize="none"
                spellCheck={false} value={username} required onChange={(event) => setUsername(event.target.value)}/>
            </div>
            <label htmlFor="operator-password">Password</label>
            <div className="demo-signin-field demo-signin-password">
              <LockKeyhole size={17} aria-hidden="true"/>
              <input id="operator-password" name="password" type={showPassword ? 'text' : 'password'}
                autoComplete="current-password" autoCapitalize="none" spellCheck={false} value={password}
                required onChange={(event) => setPassword(event.target.value)}/>
              <button type="button" aria-label={showPassword ? 'Hide password' : 'Show password'}
                aria-controls="operator-password" onClick={() => setShowPassword((value) => !value)}>
                {showPassword ? <EyeOff size={18} aria-hidden="true"/> : <Eye size={18} aria-hidden="true"/>}
              </button>
            </div>
            {error && <p className="notice error" role="alert">{error}</p>}
            <button type="submit" className="primary demo-signin-submit" disabled={busy}>
              {busy ? 'Signing in…' : 'Sign in'}<ArrowRight size={18} aria-hidden="true"/>
            </button>
          </form>
          <div className="demo-signin-account">
            <span><KeyRound size={16} aria-hidden="true"/>Local demo account</span>
            <button type="button" disabled={busy} onClick={() => {
              setUsername('operator')
              setPassword('trace-demo')
              setShowPassword(false)
            }}>Fill credentials</button>
          </div>
        </div>
      </section>

      <section className="demo-signin-preview" aria-labelledby="sign-in-preview-title">
        <div className="demo-signin-preview-copy">
          <h2 id="sign-in-preview-title">Multi-camera search</h2>
          <p>Review candidate sightings and trace a route across cameras at the search site.</p>
        </div>
        <figure className="signin-network">
          <div className="signin-network-header" aria-hidden="true"><Layers size={14}/> Example camera network</div>
          <div className="signin-network-diagram" aria-hidden="true">
            <div className="signin-network-floor">
              <span className="signin-network-shelves"/>
              <span className="signin-network-path"/>
              {['001', '002', '003', '004'].map((id) => <span key={id} className={'signin-network-node node-' + id}>
                <span className="signin-network-camera"><Camera size={15}/></span><span>CAM {id}</span>
              </span>)}
            </div>
          </div>
          <figcaption><span className="signin-network-line" aria-hidden="true"/>Illustrative camera route</figcaption>
        </figure>
      </section>
    </div>
  </main>
}
