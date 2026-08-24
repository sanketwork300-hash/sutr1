import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { CheckCircle2, XCircle } from 'lucide-react'

/**
 * Where a connected-account authorization lands.
 *
 * Its whole job is to tell whoever opened it what happened and get out of the
 * way. When there is no opener — the popup was blocked and the flow ran in
 * this tab — it falls back to sending the user to Settings, where the same
 * result is visible.
 */
export default function ConnectionCallbackPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const provider = params.get('connection') ?? ''
  const status = params.get('connection_status') ?? 'error'
  const detail = params.get('connection_error') ?? ''
  // Whether this window was opened by the builder is knowable at first render,
  // so it seeds the state instead of being discovered in an effect.
  const [closing, setClosing] = useState(
    () => typeof window !== 'undefined' && Boolean(window.opener) && window.opener !== window,
  )

  useEffect(() => {
    const message = { source: 'sutr-connection', provider, status, detail }
    if (window.opener && window.opener !== window) {
      window.opener.postMessage(message, window.location.origin)
      window.close()
      // close() is a request, not a guarantee: a tab the script did not open
      // stays put, so show something rather than a blank page.
      const timer = window.setTimeout(() => setClosing(false), 600)
      return () => window.clearTimeout(timer)
    }
    const timer = window.setTimeout(() => navigate('/settings', { replace: true }), 1800)
    return () => window.clearTimeout(timer)
  }, [detail, navigate, provider, status])

  const connected = status === 'connected'
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--bg)',
        padding: 24,
      }}
    >
      <div style={{ maxWidth: 380, textAlign: 'center', color: 'var(--text)' }}>
        {connected ? (
          <CheckCircle2 size={26} style={{ color: 'var(--badge-green-text)' }} />
        ) : (
          <XCircle size={26} style={{ color: 'var(--badge-red-text)' }} />
        )}
        <h1 style={{ fontSize: 16, fontWeight: 600, margin: '10px 0 4px' }}>
          {connected ? `${provider} connected` : `${provider} was not connected`}
        </h1>
        <p style={{ fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.6, margin: 0 }}>
          {connected
            ? closing
              ? 'Finishing up…'
              : 'You can close this window and return to sutr.'
            : detail || 'The authorization was cancelled.'}
        </p>
      </div>
    </div>
  )
}
