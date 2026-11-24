import { useRef, useState, type ReactNode } from 'react'
import { Aperture, ArrowRight, Camera, Eye, EyeOff, KeyRound, Layers, LockKeyhole, UserRound } from 'lucide-react'

type SignInProps = {
  signIn: (username: string, password: string) => void
  busy: boolean
  error: string
  notice?: string
  demo?: boolean
}

export function SignIn({ signIn, busy, error, notice, demo }: SignInProps) {
  const username = useRef<HTMLInputElement>(null)
  const password = useRef<HTMLInputElement>(null)
  const [showPassword, setShowPassword] = useState(false)

  return <SignInLayout title={demo ? 'Operator sign-in' : 'Sign in to TRACE'}
    description={demo ? 'Open your case workspace.' : 'Use your JAVES-AI account to open your case workspace.'}>
    <form className="form demo-signin-form" aria-busy={busy} onSubmit={(event) => {
      event.preventDefault()
      if (busy || !username.current || !password.current) return
      const value = password.current.value
      password.current.value = ''
      setShowPassword(false)
      signIn(username.current.value, value)
    }}>
      <label htmlFor="operator-username">Username</label>
      <div className="demo-signin-field">
        <UserRound size={17} aria-hidden="true"/>
        <input id="operator-username" name="username" autoComplete="username" autoCapitalize="none"
          spellCheck={false} ref={username} required disabled={busy}/>
      </div>
      <label htmlFor="operator-password">Password</label>
      <div className="demo-signin-field demo-signin-password">
        <LockKeyhole size={17} aria-hidden="true"/>
        <input id="operator-password" name="password" type={showPassword ? 'text' : 'password'}
          autoComplete="current-password" autoCapitalize="none" spellCheck={false} ref={password}
          required disabled={busy}/>
        <button type="button" aria-label={showPassword ? 'Hide password' : 'Show password'}
          aria-controls="operator-password" disabled={busy} onClick={() => setShowPassword((value) => !value)}>
          {showPassword ? <EyeOff size={18} aria-hidden="true"/> : <Eye size={18} aria-hidden="true"/>}
        </button>
      </div>
      {error && <p className="notice error" role="alert">{error}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      <button type="submit" className="primary demo-signin-submit" disabled={busy}>
        {busy ? 'Signing in…' : 'Sign in'}<ArrowRight size={18} aria-hidden="true"/>
      </button>
    </form>
    {demo && <div className="demo-signin-account">
      <span><KeyRound size={16} aria-hidden="true"/>Local demo account</span>
      <button type="button" disabled={busy} onClick={() => {
        if (!username.current || !password.current) return
        username.current.value = 'operator'
        password.current.value = 'trace-demo'
        setShowPassword(false)
      }}>Fill credentials</button>
    </div>}
  </SignInLayout>
}

export function SignInLayout({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return <main className="trace-console demo-signin">
    <div className="demo-signin-layout">
      <section className="demo-signin-access" aria-labelledby="sign-in-title">
        <div className="brand">
          <span className="brand-mark"><Aperture size={23} aria-hidden="true"/></span>
          <span><strong>TRACE</strong><small>OPERATIONS</small></span>
        </div>
        <div className="demo-signin-content">
          <header>
            <h1 id="sign-in-title">{title}</h1>
            <p>{description}</p>
          </header>
          {children}
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
