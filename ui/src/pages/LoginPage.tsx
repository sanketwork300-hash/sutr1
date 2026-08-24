import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { GoogleLoginButton } from '@/components/GoogleLoginButton'
import { BrandLockup } from '@/components/shell/BrandMark'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ApiError, api, type EmailVerificationRequiredDetail } from '@/api/client'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { useAuthStore } from '@/stores/auth'
import { useEmailVerificationStore } from '@/stores/emailVerification'

function getVerificationDetail(err: unknown): EmailVerificationRequiredDetail | null {
  if (!(err instanceof ApiError)) return null
  if (typeof err.body !== 'object' || err.body === null || !('detail' in err.body)) return null

  const detail = err.body.detail
  if (typeof detail !== 'object' || detail === null) return null
  if ((detail as { error?: unknown }).error !== 'email_verification_required') return null

  return detail as EmailVerificationRequiredDetail
}

function getTotpError(err: unknown): 'totp_required' | 'totp_invalid' | null {
  if (!(err instanceof ApiError)) return null
  if (typeof err.body !== 'object' || err.body === null || !('detail' in err.body)) return null
  const detail = (err.body as { detail?: unknown }).detail
  if (typeof detail !== 'object' || detail === null) return null
  const code = (detail as { error?: unknown }).error
  return code === 'totp_required' || code === 'totp_invalid' ? code : null
}

export default function LoginPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const setAuth = useAuthStore((s) => s.setAuth)
  const clearPendingVerification = useEmailVerificationStore((s) => s.clearPendingVerification)
  const startPendingVerification = useEmailVerificationStore((s) => s.startPendingVerification)
  const isDesktop = useMediaQuery('(min-width: 900px)')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [totpStep, setTotpStep] = useState(false)
  const [totpCode, setTotpCode] = useState('')
  const redirect = searchParams.get('redirect')
  const signupHref = redirect ? `/signup?redirect=${encodeURIComponent(redirect)}` : '/signup'
  const googleError = searchParams.get('google_error')

  async function onSubmit(e: { preventDefault(): void }) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await api.auth.login(email, password, totpCode || undefined)
      clearPendingVerification()
      setAuth(res.access_token)
      navigate(redirect || '/app')
    } catch (err) {
      const verificationDetail = getVerificationDetail(err)
      if (verificationDetail) {
        startPendingVerification({
          email: verificationDetail.email,
          verificationToken: verificationDetail.verification_token,
          resendAvailableAt: verificationDetail.resend_available_at,
          redirect,
        })
        navigate('/verify-email')
        return
      }
      const totpError = getTotpError(err)
      if (totpError === 'totp_required') {
        setTotpStep(true)
        setTotpCode('')
        return
      }
      if (totpError === 'totp_invalid') {
        setTotpStep(true)
        setTotpCode('')
        setError("That code didn't match — try again with a fresh one.")
        return
      }
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  const formContent = (
    <div style={formInnerStyle}>
      <div style={{ marginBottom: 28 }}>
        <Link
          to="/"
          aria-label="Swaraj Sutr home"
          style={{ display: 'inline-block', marginBottom: 10 }}
        >
          <BrandLockup size={22} />
        </Link>
        <h1
          style={{
            fontSize: isDesktop ? 26 : 24,
            fontWeight: 700,
            color: 'var(--text)',
            margin: 0,
            marginBottom: 6,
          }}
        >
          Welcome back
        </h1>
        <p style={{ fontSize: 14, color: 'var(--text-dim)', margin: 0 }}>Sign in to your account</p>
      </div>

      <div style={{ marginBottom: 16 }}>
        <GoogleLoginButton />
        {googleError && (
          <p style={{ fontSize: 12, color: 'var(--red)', margin: '8px 0 0' }}>
            {googleErrorMessage(googleError)}
          </p>
        )}
      </div>

      <div style={dividerStyle}>
        <span style={dividerLineStyle} />
        <span style={dividerLabelStyle}>or</span>
        <span style={dividerLineStyle} />
      </div>

      <form onSubmit={onSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {totpStep ? (
          <div>
            <Label htmlFor="totp" style={labelStyle}>
              Two-factor code
            </Label>
            <Input
              id="totp"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="6-digit code or recovery code"
              value={totpCode}
              onChange={(e) => setTotpCode(e.target.value)}
              required
              autoFocus
              style={inputStyle}
            />
            <p style={{ fontSize: 11, color: 'var(--text-faint)', margin: '6px 0 0' }}>
              Enter the code from your authenticator app, or one of your recovery codes.
            </p>
          </div>
        ) : (
          <>
            <div>
              <Label htmlFor="email" style={labelStyle}>
                Email
              </Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoFocus
                style={inputStyle}
              />
            </div>
            <div>
              <Label htmlFor="password" style={labelStyle}>
                Password
              </Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                style={inputStyle}
              />
            </div>
          </>
        )}

        {error && <p style={{ fontSize: 12, color: 'var(--red)', margin: 0 }}>{error}</p>}

        <Button
          type="submit"
          disabled={loading}
          style={{ width: '100%', marginTop: 4, height: 40 }}
        >
          {loading ? 'Signing in...' : totpStep ? 'Verify' : 'Sign in'}
        </Button>
        {totpStep && (
          <button
            type="button"
            onClick={() => {
              setTotpStep(false)
              setTotpCode('')
              setError('')
            }}
            style={{
              background: 'none',
              border: 'none',
              padding: 0,
              cursor: 'pointer',
              fontSize: 12,
              color: 'var(--blue)',
            }}
          >
            Back to password
          </button>
        )}
      </form>

      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
          marginTop: 16,
          textAlign: 'center',
        }}
      >
        <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: 0 }}>
          <Link to="/forgot-password" style={linkStyle}>
            Forgot your password?
          </Link>
        </p>
        <p style={{ fontSize: 12, color: 'var(--text-faint)', margin: 0 }}>
          Don't have an account?{' '}
          <Link to={signupHref} style={linkStyle}>
            Sign up
          </Link>
        </p>
      </div>
    </div>
  )

  if (!isDesktop) {
    return (
      <div style={mobilePageStyle}>
        <div style={mobileCardStyle}>{formContent}</div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', height: '100vh', overflow: 'hidden' }}>
      <div style={formPanelStyle}>{formContent}</div>
      <div style={promoPanelStyle}>
        <div style={{ maxWidth: 380, display: 'flex', flexDirection: 'column', gap: 18 }}>
          <span className="sutr-eyebrow sutr-eyebrow--brand">Swaraj Sutr</span>
          <p style={promoLineStyle}>
            The infrastructure layer for{' '}
            <span style={{ color: 'var(--text)', fontWeight: 600 }}>governed AI agents</span>.
          </p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {['Identity', 'Policy', 'Approval', 'Execution', 'Audit'].map((step, index, all) => (
              <span key={step} style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <span
                  style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 11,
                    color: 'var(--text-dim)',
                    border: '1px solid var(--border)',
                    background: 'var(--surface)',
                    borderRadius: 'var(--r-sm)',
                    padding: '4px 9px',
                  }}
                >
                  {step}
                </span>
                {index < all.length - 1 ? (
                  <span style={{ color: 'var(--text-faint)' }}>→</span>
                ) : null}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

const mobilePageStyle: React.CSSProperties = {
  minHeight: '100dvh',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'var(--bg)',
  padding: '24px 16px',
  boxSizing: 'border-box',
}

const mobileCardStyle: React.CSSProperties = {
  width: '100%',
  maxWidth: 400,
  padding: 'clamp(24px, 5vw, 40px)',
  background: 'var(--content-bg)',
  border: '1px solid var(--border)',
  borderRadius: 12,
  boxSizing: 'border-box',
}

const formPanelStyle: React.CSSProperties = {
  flex: '0 0 55%',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'var(--content-bg)',
  borderRight: '1px solid var(--border)',
  padding: '48px 32px',
  overflowY: 'auto',
}

const formInnerStyle: React.CSSProperties = {
  width: '100%',
  maxWidth: 380,
}

const promoPanelStyle: React.CSSProperties = {
  flex: 1,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'var(--bg)',
  padding: 48,
  position: 'relative',
}

const promoLineStyle: React.CSSProperties = {
  fontSize: 24,
  fontWeight: 400,
  lineHeight: 1.35,
  letterSpacing: '-0.02em',
  color: 'var(--text-dim)',
  margin: 0,
}

const labelStyle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 500,
  color: 'var(--text-dim)',
  marginBottom: 6,
  display: 'block',
}

const inputStyle: React.CSSProperties = {
  background: 'var(--input-bg)',
  border: '1px solid var(--border)',
  color: 'var(--text)',
  fontSize: 13,
}

const linkStyle: React.CSSProperties = {
  color: 'var(--blue)',
  textDecoration: 'none',
}

const dividerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  margin: '0 0 16px',
}

const dividerLineStyle: React.CSSProperties = {
  flex: 1,
  height: 1,
  background: 'var(--border)',
}

const dividerLabelStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--text-faint)',
  textTransform: 'uppercase',
  letterSpacing: 0.6,
}

const GOOGLE_ERROR_MESSAGES: Record<string, string> = {
  invalid_state: 'Your Google sign-in link expired. Please try again.',
  missing_code: 'Google did not return an authorization code. Please try again.',
  token_exchange_failed: 'Google sign-in failed during token exchange. Please try again.',
  missing_profile: 'Google did not return the expected profile. Please try again.',
  signups_disabled: 'New sign-ups are disabled on this server.',
  not_on_waitlist: 'Email not on waitlist.',
  self_hosted_org_exists: 'This server already has an organization.',
  account_disabled: 'This account is disabled. Contact an administrator.',
  access_denied: 'You denied access on the Google consent screen.',
  login_failed: 'Google sign-in failed. Please try again.',
}

function googleErrorMessage(code: string): string {
  return GOOGLE_ERROR_MESSAGES[code] ?? 'Google sign-in failed. Please try again.'
}
