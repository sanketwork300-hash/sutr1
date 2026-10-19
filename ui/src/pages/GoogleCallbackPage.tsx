import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ApiError, api } from '@/api/client'
import {
  clearGoogleCallbackFragment,
  parseFragment,
  readGoogleCallbackFragment,
} from '@/lib/authFragment'
import { consumeGoogleNonce } from '@/lib/googleNonce'
import { useAuthStore } from '@/stores/auth'
import { useEmailVerificationStore } from '@/stores/emailVerification'

const ERROR_MESSAGES: Record<string, string> = {
  invalid_state: 'Your Google sign-in link has expired. Please try again.',
  missing_code: 'Google did not return an authorization code. Please try again.',
  token_exchange_failed: 'Google sign-in failed during token exchange. Please try again.',
  missing_profile: 'Google did not return the expected profile. Please try again.',
  signups_disabled:
    'New sign-ups are disabled on this server. Ask an admin to create your account.',
  not_on_waitlist: 'Email not on waitlist.',
  self_hosted_org_exists:
    'This server already has an organization. Sign in with the existing owner account.',
  account_disabled: 'Your account is disabled. Contact an administrator.',
  access_denied: 'You denied access on the Google consent screen.',
  login_failed: 'Google sign-in failed. Please try again.',
  nonce_mismatch:
    'This sign-in was not started from this browser tab, so it was not completed. Please sign in again.',
}

/**
 * What the callback arrived with, decided once.
 *
 * The fragment was lifted out of the URL at boot (see `lib/authFragment`),
 * and the nonce stored when the sign-in started is spent here, so the
 * decision is computed in a state initialiser rather than an effect: it must
 * happen exactly once per page, and the outcome must survive re-renders.
 */
type Outcome =
  | { kind: 'token'; token: string }
  | { kind: 'totp'; challenge: string }
  | { kind: 'error'; code: string }

function decide(): Outcome {
  const params = parseFragment(readGoogleCallbackFragment())
  const queryError = new URLSearchParams(window.location.search).get('google_error')
  const error = params.error || queryError
  if (error) return { kind: 'error', code: error }

  const token = params.access_token
  const challenge = params.totp_challenge
  if (!token && !challenge) return { kind: 'error', code: 'login_failed' }

  // Whatever the fragment carries, it only counts if this tab asked for it.
  // Otherwise any `#access_token=` link would silently sign the user into
  // someone else's workspace.
  if (!consumeGoogleNonce(params.nonce)) return { kind: 'error', code: 'nonce_mismatch' }

  if (token) return { kind: 'token', token }
  return { kind: 'totp', challenge: challenge! }
}

export default function GoogleCallbackPage() {
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)
  const clearPendingVerification = useEmailVerificationStore((s) => s.clearPendingVerification)
  const [outcome] = useState<Outcome>(decide)

  useEffect(() => {
    if (outcome.kind !== 'token') return
    clearGoogleCallbackFragment()
    clearPendingVerification()
    setAuth(outcome.token)
    navigate('/app/integrations', { replace: true })
  }, [outcome, setAuth, clearPendingVerification, navigate])

  if (outcome.kind === 'error') {
    return (
      <div style={wrapperStyle}>
        <div style={cardStyle}>
          <h1 style={headingStyle}>Google sign-in failed</h1>
          <p style={bodyStyle}>{ERROR_MESSAGES[outcome.code] ?? 'Please try again.'}</p>
          <Link to="/login" style={linkStyle}>
            Back to sign in
          </Link>
        </div>
      </div>
    )
  }

  if (outcome.kind === 'totp') {
    return (
      <TotpChallenge
        challenge={outcome.challenge}
        onSuccess={(token) => {
          clearGoogleCallbackFragment()
          clearPendingVerification()
          setAuth(token)
          navigate('/app/integrations', { replace: true })
        }}
      />
    )
  }

  return (
    <div style={wrapperStyle}>
      <p style={bodyStyle}>Signing you in…</p>
    </div>
  )
}

function totpErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) return 'Too many attempts. Wait a few minutes, then try again.'
    const detail = (err.body as { detail?: { error?: unknown } } | null)?.detail
    if (detail && typeof detail === 'object' && detail.error === 'totp_invalid') {
      return "That code didn't match — try again with a fresh one."
    }
    if (err.status === 401 || err.status === 403) {
      return 'This sign-in has expired. Please start again.'
    }
  }
  return err instanceof Error ? err.message : 'Verification failed'
}

/**
 * The second factor for a Google sign-in. Google only proved the email; an
 * account with an authenticator enrolled still has to present it, exactly as
 * it would after a password.
 */
function TotpChallenge({
  challenge,
  onSuccess,
}: {
  challenge: string
  onSuccess: (token: string) => void
}) {
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function onSubmit(e: { preventDefault(): void }) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await api.auth.totpChallenge(challenge, code.trim())
      onSuccess(res.access_token)
    } catch (err) {
      setCode('')
      setError(totpErrorMessage(err))
      setLoading(false)
    }
  }

  return (
    <div style={wrapperStyle}>
      <div style={{ ...cardStyle, textAlign: 'left' }}>
        <h1 style={headingStyle}>Two-factor code</h1>
        <p style={bodyStyle}>
          Enter the code from your authenticator app, or one of your recovery codes.
        </p>
        <form onSubmit={onSubmit} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div>
            <Label htmlFor="totp" style={labelStyle}>
              Code
            </Label>
            <Input
              id="totp"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="6-digit code or recovery code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              required
              autoFocus
              style={inputStyle}
            />
          </div>
          {error && <p style={{ fontSize: 12, color: 'var(--red)', margin: 0 }}>{error}</p>}
          <Button
            type="submit"
            disabled={loading || !code.trim()}
            style={{ width: '100%', height: 40 }}
          >
            {loading ? 'Verifying...' : 'Verify'}
          </Button>
        </form>
        <p style={{ ...bodyStyle, margin: '16px 0 0', textAlign: 'center' }}>
          <Link to="/login" style={linkStyle}>
            Back to sign in
          </Link>
        </p>
      </div>
    </div>
  )
}

const wrapperStyle: React.CSSProperties = {
  minHeight: '100dvh',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'var(--bg)',
  padding: '24px 16px',
  boxSizing: 'border-box',
}

const cardStyle: React.CSSProperties = {
  width: '100%',
  maxWidth: 380,
  padding: 32,
  background: 'var(--content-bg)',
  border: '1px solid var(--border)',
  borderRadius: 12,
  textAlign: 'center',
}

const headingStyle: React.CSSProperties = {
  fontSize: 20,
  fontWeight: 700,
  color: 'var(--text)',
  margin: '0 0 8px',
}

const bodyStyle: React.CSSProperties = {
  fontSize: 14,
  color: 'var(--text-dim)',
  margin: '0 0 16px',
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
  fontSize: 13,
  color: 'var(--blue)',
  textDecoration: 'none',
}
