import { useEffect, useRef, useState } from 'react'
import { Copy, ExternalLink, Loader2 } from 'lucide-react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { api, type AuthorizeResult } from '@/api/client'

/**
 * The AWS half of "connect with OAuth".
 *
 * AWS has no OAuth interface to its own service APIs, so this is the OAuth 2.0
 * device authorization grant against IAM Identity Center: sutr shows a code,
 * the user approves it in the AWS access portal, and sutr polls until the
 * token appears. The code is shown *and* embedded in the link, because the
 * verification URL with the code prefilled is the path people actually take
 * and the bare code is what they need if they open the portal themselves.
 */
export function AwsDeviceDialog({
  authorization,
  onConnected,
  onClose,
}: {
  authorization: AuthorizeResult | null
  onConnected: () => void
  onClose: () => void
}) {
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const [done, setDone] = useState(false)
  const state = authorization?.state
  const interval = authorization?.interval
  // The callback changes identity on every parent render; holding it in a ref
  // keeps the polling effect keyed on the authorization alone, so a re-render
  // never restarts the timer mid-flow.
  const onConnectedRef = useRef(onConnected)
  useEffect(() => {
    onConnectedRef.current = onConnected
  }, [onConnected])

  useEffect(() => {
    if (!state) return
    let stopped = false
    async function poll() {
      if (stopped || !state) return
      try {
        const result = await api.connections.pollAws(state)
        if (result.status === 'connected' && !stopped) {
          stopped = true
          setDone(true)
          onConnectedRef.current()
        }
      } catch (e) {
        if (stopped) return
        stopped = true
        setError(e instanceof Error ? e.message : 'AWS refused the authorization')
      }
    }
    const timer = window.setInterval(() => void poll(), Math.max(interval ?? 5, 2) * 1000)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [interval, state])

  if (!authorization) return null
  const waiting = !done && !error

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle>Authorize AWS</DialogTitle>
          <p style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.55 }}>
            {authorization.grant_summary}
          </p>
        </DialogHeader>

        <div style={{ paddingTop: 6 }}>
          <div style={labelStyle}>Confirmation code</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
            <code
              style={{
                flex: 1,
                fontFamily: 'var(--font-mono)',
                fontSize: 20,
                letterSpacing: 3,
                padding: '10px 12px',
                borderRadius: 8,
                background: 'var(--code-bg)',
                color: 'var(--code-text)',
                textAlign: 'center',
              }}
            >
              {authorization.user_code}
            </code>
            <Button
              size="sm"
              variant="outline"
              onClick={async () => {
                await navigator.clipboard.writeText(authorization.user_code ?? '')
                setCopied(true)
                window.setTimeout(() => setCopied(false), 1500)
              }}
            >
              <Copy size={13} style={{ marginRight: 5 }} />
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>

          <ol
            style={{
              margin: '0 0 14px',
              paddingLeft: 18,
              fontSize: 12.5,
              color: 'var(--text-dim)',
              lineHeight: 1.9,
            }}
          >
            <li>Open the AWS access portal and sign in.</li>
            <li>Confirm the code above matches what AWS shows.</li>
            <li>Choose Allow. This window finishes on its own.</li>
          </ol>

          <a
            href={authorization.verification_uri_complete}
            target="_blank"
            rel="noreferrer"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 12.5,
              color: 'var(--text)',
              textDecoration: 'underline',
            }}
          >
            <ExternalLink size={13} /> Open the AWS access portal
          </a>

          <div
            style={{
              marginTop: 16,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontSize: 12,
              color: error ? 'var(--badge-red-text)' : 'var(--text-faint)',
            }}
          >
            {waiting && <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />}
            <span>{error || (waiting ? 'Waiting for approval…' : 'Connected.')}</span>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

const labelStyle: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 600,
  color: 'var(--text-faint)',
  textTransform: 'uppercase',
  letterSpacing: 0.4,
  marginBottom: 6,
}
