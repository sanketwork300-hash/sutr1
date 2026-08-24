import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { api, type InvitationPreview } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

/**
 * Landing page for org invitation links (/join?token=...).
 *
 * New users set a password and land signed in; existing users accept the
 * invitation (logging in first if needed) and the org appears in their account.
 */
export default function JoinPage() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token') || ''
  const authToken = useAuthStore((s) => s.token)
  const setAuth = useAuthStore((s) => s.setAuth)

  const [preview, setPreview] = useState<InvitationPreview | null>(null)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!token) {
      setError('This invitation link is missing its token.')
      setLoading(false)
      return
    }
    api.orgInvitations
      .preview(token)
      .then(setPreview)
      .catch((err) =>
        setError(err instanceof Error ? err.message : 'Invitation not found or expired'),
      )
      .finally(() => setLoading(false))
  }, [token])

  async function accept(withPassword: boolean) {
    setError('')
    setSubmitting(true)
    try {
      const res = await api.orgInvitations.accept(token, withPassword ? password : undefined)
      if (res.access_token) setAuth(res.access_token)
      if (res.access_token || authToken) {
        navigate('/app/integrations')
      } else {
        navigate(`/login?redirect=${encodeURIComponent('/app/integrations')}`)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not accept the invitation')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div style={pageStyle}>
      <div style={cardStyle}>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: 'var(--text)', margin: '0 0 6px' }}>
          Join organization
        </h1>
        {loading && <p style={dimStyle}>Loading invitation…</p>}

        {!loading && preview && (
          <>
            <p style={dimStyle}>
              You've been invited to join <strong>{preview.org_name}</strong> as{' '}
              <strong>{preview.role}</strong> with the email <strong>{preview.email}</strong>.
            </p>

            {preview.account_exists ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 16 }}>
                {!authToken && (
                  <p style={dimStyle}>
                    You already have an account —{' '}
                    <Link
                      to={`/login?redirect=${encodeURIComponent(`/join?token=${token}`)}`}
                      style={{ color: 'var(--blue)', textDecoration: 'none' }}
                    >
                      sign in
                    </Link>{' '}
                    first, then accept.
                  </p>
                )}
                <Button
                  disabled={submitting}
                  onClick={() => accept(false)}
                  style={{ width: '100%', height: 40 }}
                >
                  {submitting ? 'Accepting…' : 'Accept invitation'}
                </Button>
              </div>
            ) : (
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  accept(true)
                }}
                style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 16 }}
              >
                <div>
                  <Label
                    htmlFor="password"
                    style={{
                      fontSize: 12,
                      fontWeight: 500,
                      color: 'var(--text-dim)',
                      marginBottom: 6,
                      display: 'block',
                    }}
                  >
                    Choose a password
                  </Label>
                  <Input
                    id="password"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    minLength={6}
                    required
                    autoFocus
                    style={{
                      background: 'var(--input-bg)',
                      border: '1px solid var(--border)',
                      color: 'var(--text)',
                      fontSize: 13,
                    }}
                  />
                </div>
                <Button type="submit" disabled={submitting} style={{ width: '100%', height: 40 }}>
                  {submitting ? 'Creating account…' : 'Create account & join'}
                </Button>
              </form>
            )}
          </>
        )}

        {error && <p style={{ fontSize: 12, color: 'var(--red)', margin: '12px 0 0' }}>{error}</p>}
      </div>
    </div>
  )
}

const pageStyle: React.CSSProperties = {
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
  maxWidth: 420,
  padding: 'clamp(24px, 5vw, 40px)',
  background: 'var(--content-bg)',
  border: '1px solid var(--border)',
  borderRadius: 12,
  boxSizing: 'border-box',
}

const dimStyle: React.CSSProperties = {
  fontSize: 14,
  color: 'var(--text-dim)',
  margin: 0,
  lineHeight: 1.5,
}
