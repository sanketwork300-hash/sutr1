import { useCallback, useEffect, useState } from 'react'
import { Check, Loader2, Lock, Plug, Search } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { api, type ConnectionProviderInfo, type GithubRepo } from '@/api/client'
import {
  useConnectionMessages,
  startRedirectConnect,
} from '@/components/connections/useProviderConnections'
import {
  Field,
  SecretInput,
} from '@/components/mcp-builder/primitives'
import {
  monoInputStyle,
} from '@/components/mcp-builder/styles'

export type GithubAuthMode = 'connection' | 'token'

/**
 * The GitHub source: authorize once, then pick a repository from a list.
 *
 * The pasted-token path is kept, not deprecated. It is the only path that
 * works for an API key caller, for a repository under an account the user
 * would rather not connect wholesale, and for a self-hosted install whose
 * operator has not registered an OAuth app. What changes is which one is
 * offered first.
 */
export function GithubSource({
  provider,
  onProviderChanged,
  mode,
  onMode,
  url,
  onUrl,
  token,
  onToken,
}: {
  provider: ConnectionProviderInfo | undefined
  onProviderChanged: () => void
  mode: GithubAuthMode
  onMode: (mode: GithubAuthMode) => void
  url: string
  onUrl: (value: string) => void
  token: string
  onToken: (value: string) => void
}) {
  const connection = provider?.connection ?? null
  const connected = Boolean(connection) && !connection?.expired
  const [connecting, setConnecting] = useState(false)
  const [connectError, setConnectError] = useState('')

  const handleMessage = useCallback(() => {
    setConnecting(false)
    onProviderChanged()
  }, [onProviderChanged])
  useConnectionMessages(handleMessage)

  async function connect() {
    setConnectError('')
    setConnecting(true)
    try {
      await startRedirectConnect('github')
    } catch (e) {
      setConnecting(false)
      setConnectError(e instanceof Error ? e.message : 'Could not start the GitHub authorization')
    }
  }

  return (
    <>
      <Field
        label="GitHub access"
        help="A connected account lets sutr list your repositories and read private ones without a token living anywhere. Nothing is written back to GitHub."
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <label style={optionRowStyle}>
            <input
              type="radio"
              name="github-auth"
              checked={mode === 'connection'}
              onChange={() => onMode('connection')}
              disabled={!provider?.configured}
            />
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={{ fontSize: 12.5, color: 'var(--text)' }}>
                {connected ? (
                  <>
                    Connected as <strong>{connection?.account_label || 'your account'}</strong>
                  </>
                ) : (
                  'Connected account (OAuth)'
                )}
              </span>
              <span style={hintStyle}>
                {!provider?.configured
                  ? provider?.reason ??
                    'GitHub OAuth is not configured on this server. Use a token instead.'
                  : connection?.expired
                    ? 'The stored authorization expired. Reconnect to continue.'
                    : connected
                      ? `Scopes granted: ${connection?.scopes || 'none reported'}`
                      : provider.grant_summary}
              </span>
            </span>
            {provider?.configured && !connected && (
              <Button size="sm" onClick={connect} disabled={connecting}>
                {connecting ? (
                  <Loader2 size={13} style={spinStyle} />
                ) : (
                  <>
                    <Plug size={13} style={{ marginRight: 5 }} /> Connect
                  </>
                )}
              </Button>
            )}
            {connected && <Check size={15} style={{ color: 'var(--badge-green-text)' }} />}
          </label>

          <label style={optionRowStyle}>
            <input
              type="radio"
              name="github-auth"
              checked={mode === 'token'}
              onChange={() => onMode('token')}
            />
            <span style={{ flex: 1, minWidth: 0 }}>
              <span style={{ fontSize: 12.5, color: 'var(--text)' }}>
                Personal access token, or none at all
              </span>
              <span style={hintStyle}>
                Public repositories need no credential. A token is only for private repos or for
                getting past GitHub&apos;s unauthenticated rate limit.
              </span>
            </span>
          </label>
        </div>
        {connectError && (
          <p style={{ margin: '8px 0 0', fontSize: 11.5, color: 'var(--badge-red-text)' }}>
            {connectError}
          </p>
        )}
      </Field>

      {mode === 'connection' && connected && (
        <RepoPicker selected={url} onSelect={onUrl} />
      )}

      <Field
        label="Repository or file URL"
        help={
          mode === 'connection' && connected
            ? 'Filled in by the picker above, or paste any of these directly:'
            : 'Any of these work — sutr figures out the rest:'
        }
        examples={[
          'https://github.com/owner/repo  (searches for openapi.yaml, swagger.json, …)',
          'https://github.com/owner/repo/tree/main/specs  (searches that folder)',
          'https://github.com/owner/repo/blob/main/openapi.yaml  (uses exactly that file)',
        ]}
      >
        <input
          value={url}
          onChange={(event) => onUrl(event.target.value)}
          placeholder="https://github.com/owner/repo"
          spellCheck={false}
          style={monoInputStyle}
        />
      </Field>

      {mode === 'token' && (
        <Field
          label="GitHub token"
          optional
          help="Create one at github.com → Settings → Developer settings → Personal access tokens, with read access to the repository's contents. It is used for this import only and never stored — re-enter it if you import again."
        >
          <SecretInput value={token} onChange={onToken} placeholder="ghp_… or github_pat_…" />
        </Field>
      )}
    </>
  )
}

/** Repositories the connected account can read, filtered as you type. */
function RepoPicker({
  selected,
  onSelect,
}: {
  selected: string
  onSelect: (url: string) => void
}) {
  const [repos, setRepos] = useState<GithubRepo[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    api.connections
      .githubRepos()
      .then((result) => {
        if (cancelled) return
        setRepos(result.repositories)
        setError('')
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not list repositories')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const needle = query.trim().toLowerCase()
  const visible = needle
    ? repos.filter((repo) => repo.full_name.toLowerCase().includes(needle))
    : repos

  return (
    <Field
      label="Your repositories"
      optional
      help="Most recently updated first. Choosing one fills in the URL below; sutr then searches it for specification files."
    >
      {error ? (
        <p style={{ margin: 0, fontSize: 12, color: 'var(--badge-red-text)' }}>{error}</p>
      ) : (
        <>
          <div style={{ position: 'relative', marginBottom: 8 }}>
            <Search
              size={13}
              style={{ position: 'absolute', left: 11, top: 11, color: 'var(--text-faint)' }}
            />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter by name"
              style={{ ...monoInputStyle, paddingLeft: 32 }}
            />
          </div>
          <div
            style={{
              maxHeight: 190,
              overflow: 'auto',
              border: '1px solid var(--border)',
              borderRadius: 8,
            }}
          >
            {loading && (
              <div style={{ padding: 12, fontSize: 12, color: 'var(--text-dim)' }}>
                <Loader2 size={13} style={{ ...spinStyle, marginRight: 6 }} />
                Loading repositories…
              </div>
            )}
            {!loading && visible.length === 0 && (
              <div style={{ padding: 12, fontSize: 12, color: 'var(--text-dim)' }}>
                No repositories match. Paste a URL below instead.
              </div>
            )}
            {visible.map((repo) => (
              <button
                key={repo.full_name}
                type="button"
                onClick={() => onSelect(repo.html_url)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  width: '100%',
                  textAlign: 'left',
                  padding: '7px 10px',
                  border: 'none',
                  borderBottom: '1px solid var(--border)',
                  background:
                    selected === repo.html_url ? 'var(--surface-hover)' : 'transparent',
                  color: 'var(--text)',
                  fontFamily: 'inherit',
                  cursor: 'pointer',
                }}
              >
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>
                  {repo.full_name}
                </span>
                {repo.private && <Lock size={11} style={{ color: 'var(--text-faint)' }} />}
                <span
                  style={{
                    marginLeft: 'auto',
                    fontSize: 10.5,
                    color: 'var(--text-faint)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    maxWidth: 220,
                  }}
                >
                  {repo.description}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </Field>
  )
}

const spinStyle: React.CSSProperties = { animation: 'spin 1s linear infinite' }

const optionRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 9,
  padding: '10px 11px',
  border: '1px solid var(--border)',
  borderRadius: 8,
  background: 'var(--surface)',
  cursor: 'pointer',
}

const hintStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 11,
  color: 'var(--text-dim)',
  lineHeight: 1.5,
  marginTop: 2,
}
