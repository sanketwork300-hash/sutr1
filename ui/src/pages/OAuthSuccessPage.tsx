import { CheckCircle2 } from 'lucide-react'
import { Button } from '@/components/ui/button'

/**
 * A static landing for a finished authorization.
 *
 * The consent page sends the browser straight to the client's callback, so
 * nothing arrives here with a code any more. This page takes no parameters:
 * an earlier version accepted `?redirect=` and loaded it, which made it a
 * place anyone could point a link at.
 */
export default function OAuthSuccessPage() {
  return (
    <div style={pageStyle}>
      <div style={cardStyle}>
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div style={iconContainerStyle}>
            <CheckCircle2 size={24} style={{ color: '#fff' }} />
          </div>
          <h1 style={headingStyle}>Connected successfully</h1>
          <p style={subtitleStyle}>Sutr</p>
        </div>

        <p style={messageStyle}>Authorization is complete. You can close this window.</p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 4 }}>
          <Button
            variant="outline"
            onClick={() => window.close()}
            style={{ width: '100%', height: 38, fontSize: 13 }}
          >
            Close window
          </Button>
        </div>
      </div>
      <p style={footerStyle}>Sutr</p>
    </div>
  )
}

const pageStyle: React.CSSProperties = {
  minHeight: '100dvh',
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'var(--bg)',
  padding: '24px 16px',
  boxSizing: 'border-box',
}

const cardStyle: React.CSSProperties = {
  width: '100%',
  maxWidth: 420,
  padding: 'clamp(24px, 5vw, 36px)',
  background: 'var(--content-bg)',
  border: '1px solid var(--border)',
  borderRadius: 12,
  boxShadow: 'var(--shadow-sm)',
  boxSizing: 'border-box',
}

const iconContainerStyle: React.CSSProperties = {
  width: 44,
  height: 44,
  borderRadius: 22,
  background: 'var(--green)',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  marginBottom: 14,
}

const headingStyle: React.CSSProperties = {
  fontSize: 15,
  fontWeight: 600,
  color: 'var(--text)',
  margin: '0 0 4px',
}

const subtitleStyle: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--text-faint)',
  margin: 0,
}

const messageStyle: React.CSSProperties = {
  fontSize: 13,
  lineHeight: 1.6,
  color: 'var(--text-dim)',
  textAlign: 'center',
  margin: '0 0 16px',
}

const footerStyle: React.CSSProperties = {
  marginTop: 16,
  fontSize: 11,
  color: 'var(--text-faint)',
  letterSpacing: '0.04em',
}
