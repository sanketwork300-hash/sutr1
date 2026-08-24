import { useCallback, useState } from 'react'
import { AlertTriangle, Cloud, Code2, Loader2, Plug } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { api, type AuthorizeResult, type ConnectionProviderInfo } from '@/api/client'
import { AwsDeviceDialog } from '@/components/connections/AwsDeviceDialog'
import {
  useConnectionMessages,
  useProviderConnections,
  startRedirectConnect,
} from '@/components/connections/useProviderConnections'

/**
 * Settings → Connected accounts.
 *
 * One row per provider this build knows about, whether or not it is set up,
 * because "GitHub is not offered here" and "GitHub is not connected yet" are
 * different problems with different fixes, and only one of them is the user's.
 */
export function ConnectedAccountsPanel() {
  const { providers, loading, error, reload } = useProviderConnections()
  const [busy, setBusy] = useState('')
  const [actionError, setActionError] = useState('')
  const [device, setDevice] = useState<AuthorizeResult | null>(null)

  const onMessage = useCallback(() => {
    void reload()
  }, [reload])
  useConnectionMessages(onMessage)

  async function connect(provider: ConnectionProviderInfo) {
    setActionError('')
    setBusy(provider.id)
    try {
      if (provider.flow === 'device') {
        setDevice(await api.connections.authorize(provider.id))
      } else {
        await startRedirectConnect(provider.id)
      }
    } catch (e) {
      setActionError(e instanceof Error ? e.message : `Could not start the ${provider.id} flow`)
    } finally {
      setBusy('')
    }
  }

  async function disconnect(provider: ConnectionProviderInfo) {
    if (!provider.connection) return
    setActionError('')
    setBusy(provider.id)
    try {
      await api.connections.disconnect(provider.connection.id)
      await reload()
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Could not disconnect')
    } finally {
      setBusy('')
    }
  }

  if (loading) {
    return <p style={mutedStyle}>Loading connected accounts…</p>
  }
  if (error) {
    return <p style={{ ...mutedStyle, color: 'var(--badge-red-text)' }}>{error}</p>
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <p style={{ ...mutedStyle, margin: 0 }}>
        Accounts sutr may act on for you: GitHub for reading OpenAPI specifications, and the
        clouds for running generated MCP servers. Disconnecting deletes the stored tokens here —
        it does not revoke the app on the provider, which only you can do from their settings.
      </p>

      {actionError && (
        <div style={errorBannerStyle}>
          <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{actionError}</span>
        </div>
      )}

      {providers.map((provider) => (
        <ProviderRow
          key={provider.id}
          provider={provider}
          busy={busy === provider.id}
          onConnect={() => connect(provider)}
          onDisconnect={() => disconnect(provider)}
        />
      ))}

      <AwsDeviceDialog
        authorization={device}
        onConnected={() => {
          setDevice(null)
          void reload()
        }}
        onClose={() => setDevice(null)}
      />
    </div>
  )
}

function ProviderRow({
  provider,
  busy,
  onConnect,
  onDisconnect,
}: {
  provider: ConnectionProviderInfo
  busy: boolean
  onConnect: () => void
  onDisconnect: () => void
}) {
  const connection = provider.connection
  return (
    <div
      style={{
        border: '1px solid var(--border)',
        borderRadius: 10,
        background: 'var(--surface)',
        padding: '13px 15px',
        display: 'flex',
        alignItems: 'flex-start',
        gap: 12,
      }}
    >
      <span style={iconStyle}>
        {provider.kind === 'deploy' ? <Cloud size={15} /> : <Code2 size={15} />}
      </span>

      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>
            {provider.display_name}
          </span>
          <span style={pillStyle}>{provider.kind === 'deploy' ? 'deploy target' : 'spec source'}</span>
          {connection?.expired && <span style={{ ...pillStyle, ...expiredPillStyle }}>expired</span>}
        </div>

        <p style={{ margin: '4px 0 0', fontSize: 11.5, color: 'var(--text-dim)', lineHeight: 1.55 }}>
          {provider.grant_summary}
        </p>

        {connection ? (
          <p style={{ margin: '6px 0 0', fontSize: 11.5, color: 'var(--text-faint)' }}>
            Connected{connection.account_label ? ` as ${connection.account_label}` : ''} ·{' '}
            {new Date(connection.updated_at).toLocaleDateString()}
            {connection.expires_at
              ? ` · token expires ${new Date(connection.expires_at).toLocaleString()}`
              : ''}
          </p>
        ) : !provider.configured ? (
          <p style={{ margin: '6px 0 0', fontSize: 11.5, color: 'var(--badge-amber-text, var(--text-faint))' }}>
            {provider.reason}
          </p>
        ) : (
          <p style={{ margin: '6px 0 0', fontSize: 11.5, color: 'var(--text-faint)' }}>
            Not connected. Scopes requested: <code>{provider.scopes}</code>
          </p>
        )}
      </div>

      <div style={{ flexShrink: 0 }}>
        {connection ? (
          <Button size="sm" variant="outline" onClick={onDisconnect} disabled={busy}>
            {busy ? <Loader2 size={13} style={spinStyle} /> : 'Disconnect'}
          </Button>
        ) : (
          <Button size="sm" onClick={onConnect} disabled={busy || !provider.configured}>
            {busy ? (
              <Loader2 size={13} style={spinStyle} />
            ) : (
              <>
                <Plug size={13} style={{ marginRight: 5 }} /> Connect
              </>
            )}
          </Button>
        )}
      </div>
    </div>
  )
}

const spinStyle: React.CSSProperties = { animation: 'spin 1s linear infinite' }

const mutedStyle: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--text-dim)',
  lineHeight: 1.6,
}

const iconStyle: React.CSSProperties = {
  width: 30,
  height: 30,
  borderRadius: 7,
  flexShrink: 0,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'var(--content-bg)',
  border: '1px solid var(--border)',
  color: 'var(--text-dim)',
}

const pillStyle: React.CSSProperties = {
  fontSize: 9.5,
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: 0.4,
  padding: '2px 6px',
  borderRadius: 4,
  background: 'var(--content-bg)',
  border: '1px solid var(--border)',
  color: 'var(--text-faint)',
}

const expiredPillStyle: React.CSSProperties = {
  background: 'var(--badge-red-bg)',
  color: 'var(--badge-red-text)',
  borderColor: 'transparent',
}

const errorBannerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  padding: '9px 12px',
  borderRadius: 8,
  fontSize: 12.5,
  lineHeight: 1.5,
  background: 'var(--badge-red-bg)',
  color: 'var(--badge-red-text)',
}
