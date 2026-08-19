import { useCallback, useEffect, useRef, useState } from 'react'
import {
  FileText,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Rocket,
  Square,
  Trash2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  api,
  type Deployment,
  type DeploymentProviderInfo,
  type OpenApiProject,
} from '@/api/client'

const STATUS_COLORS: Record<string, { bg: string; text: string }> = {
  running: { bg: 'var(--badge-green-bg, #e6f6ec)', text: 'var(--badge-green-text, #1a7f37)' },
  building: { bg: 'var(--badge-yellow-bg, #fff8e1)', text: 'var(--badge-yellow-text, #9a6700)' },
  queued: { bg: 'var(--badge-yellow-bg, #fff8e1)', text: 'var(--badge-yellow-text, #9a6700)' },
  stopped: { bg: 'var(--surface)', text: 'var(--text-dim)' },
  failed: { bg: 'var(--badge-red-bg, #ffebe9)', text: 'var(--badge-red-text, #cf222e)' },
}

export default function DeploymentsPage() {
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [providers, setProviders] = useState<DeploymentProviderInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [logsFor, setLogsFor] = useState<Deployment | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      const rows = await api.deployments.list()
      setDeployments(rows)
      setError('')
      return rows
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load deployments')
      return []
    }
  }, [])

  useEffect(() => {
    Promise.all([refresh(), api.deployments.providers().then(setProviders)]).finally(() =>
      setLoading(false),
    )
  }, [refresh])

  // Poll while anything is queued/building so the status pill flips on its own.
  useEffect(() => {
    const active = deployments.some((d) => d.status === 'queued' || d.status === 'building')
    if (active && pollRef.current === null) {
      pollRef.current = window.setInterval(refresh, 3000)
    }
    if (!active && pollRef.current !== null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
    return () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [deployments, refresh])

  async function act(id: string, action: 'start' | 'stop' | 'delete') {
    setBusy(id)
    setError('')
    try {
      if (action === 'delete') {
        await api.deployments.remove(id)
      } else {
        await api.deployments[action](id)
      }
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : `Failed to ${action}`)
    } finally {
      setBusy(null)
    }
  }

  const dockerProvider = providers.find((p) => p.id === 'docker')

  return (
    <div style={{ flex: 1, overflow: 'auto', background: 'var(--bg)', padding: '28px 24px 80px' }}>
      <div style={{ maxWidth: 860, margin: '0 auto' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 18,
          }}
        >
          <div>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600, color: 'var(--text)' }}>
              Deployments
            </h1>
            <p style={{ margin: '3px 0 0', fontSize: 12.5, color: 'var(--text-dim)' }}>
              Generated MCP servers running on your infrastructure.
            </p>
          </div>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus size={14} style={{ marginRight: 5 }} /> New deployment
          </Button>
        </div>

        {dockerProvider && !dockerProvider.enabled && (
          <Banner tone="warn">{dockerProvider.reason}</Banner>
        )}
        {error && <Banner tone="error">{error}</Banner>}

        {loading ? (
          <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-faint)' }}>
            <Loader2 size={18} style={{ animation: 'spin 1s linear infinite' }} />
          </div>
        ) : deployments.length === 0 ? (
          <EmptyState onCreate={() => setCreateOpen(true)} />
        ) : (
          <div
            style={{
              background: 'var(--content-bg)',
              border: '1px solid var(--border)',
              borderRadius: 10,
              overflow: 'hidden',
            }}
          >
            {deployments.map((d, i) => (
              <div
                key={d.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '12px 16px',
                  borderTop: i > 0 ? '1px solid var(--border)' : 'none',
                }}
              >
                <Rocket size={16} style={{ color: 'var(--text-faint)', flexShrink: 0 }} />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--text)' }}>
                      {d.name}
                    </span>
                    <StatusPill status={d.status} />
                    <span style={{ fontSize: 11, color: 'var(--text-faint)' }}>
                      {d.tool_count} tool{d.tool_count === 1 ? '' : 's'} · {d.provider}
                    </span>
                  </div>
                  <div
                    style={{
                      fontSize: 11.5,
                      fontFamily: 'var(--font-mono)',
                      color: d.status === 'failed' ? 'var(--red, #cf222e)' : 'var(--text-dim)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      marginTop: 2,
                    }}
                  >
                    {d.status === 'failed' ? (d.error ?? 'failed') : (d.url ?? '…')}
                  </div>
                </div>

                <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                  {busy === d.id ? (
                    <Loader2
                      size={14}
                      style={{ animation: 'spin 1s linear infinite', color: 'var(--text-faint)' }}
                    />
                  ) : (
                    <>
                      <IconButton
                        title="Logs"
                        onClick={() => setLogsFor(d)}
                        icon={<FileText size={13} />}
                      />
                      {d.status === 'running' ? (
                        <IconButton
                          title="Stop"
                          onClick={() => act(d.id, 'stop')}
                          icon={<Square size={13} />}
                        />
                      ) : d.status === 'stopped' ? (
                        <IconButton
                          title="Start"
                          onClick={() => act(d.id, 'start')}
                          icon={<Play size={13} />}
                        />
                      ) : null}
                      <IconButton
                        title="Delete"
                        onClick={() => {
                          if (window.confirm(`Delete deployment '${d.name}'?`)) {
                            act(d.id, 'delete')
                          }
                        }}
                        icon={<Trash2 size={13} />}
                        danger
                      />
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <CreateDeploymentDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          setCreateOpen(false)
          refresh()
        }}
      />
      <LogsDialog deployment={logsFor} onClose={() => setLogsFor(null)} />
    </div>
  )
}

function CreateDeploymentDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean
  onClose: () => void
  onCreated: () => void
}) {
  const [projects, setProjects] = useState<OpenApiProject[]>([])
  const [projectId, setProjectId] = useState('')
  const [name, setName] = useState('')
  const [token, setToken] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (open) {
      api.openapi.list().then((rows) => {
        setProjects(rows)
        if (rows.length > 0 && !projectId) setProjectId(rows[0].id)
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  async function submit(e: { preventDefault: () => void }) {
    e.preventDefault()
    if (!projectId) {
      setError('Import an OpenAPI project first (Integrations → New → OpenAPI spec)')
      return
    }
    if (!name.trim()) {
      setError('Give the deployment a name')
      return
    }
    setSaving(true)
    setError('')
    try {
      await api.deployments.create({
        project_id: projectId,
        name: name.trim(),
        provider: 'docker',
        token: token || null,
      })
      setName('')
      setToken('')
      onCreated()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create deployment')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>New deployment</DialogTitle>
          <p style={{ margin: '4px 0 0', fontSize: 12, color: 'var(--text-dim)' }}>
            Runs a generated MCP server as a local Docker container, bound to 127.0.0.1.
          </p>
        </DialogHeader>
        <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <label style={fieldLabelStyle}>
            OpenAPI project
            <select
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              style={inputStyle}
            >
              {projects.length === 0 && <option value="">No projects imported yet</option>}
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.operation_count} ops)
                </option>
              ))}
            </select>
          </label>
          <label style={fieldLabelStyle}>
            Name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Petstore prod"
              style={inputStyle}
            />
          </label>
          <label style={fieldLabelStyle}>
            Upstream API token (optional)
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="Injected as an env var at runtime"
              autoComplete="off"
              style={{ ...inputStyle, fontFamily: 'var(--font-mono)', fontSize: 12.5 }}
            />
          </label>
          {error && (
            <div style={{ fontSize: 12, color: 'var(--badge-red-text, #cf222e)' }}>{error}</div>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
            <Button type="button" variant="ghost" size="sm" onClick={onClose} disabled={saving}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={saving}>
              {saving ? (
                <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                'Deploy'
              )}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function LogsDialog({
  deployment,
  onClose,
}: {
  deployment: Deployment | null
  onClose: () => void
}) {
  const [logs, setLogs] = useState('')
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    if (!deployment) return
    setLoading(true)
    try {
      const result = await api.deployments.logs(deployment.id)
      setLogs(result.logs || '(no output yet)')
    } catch (e) {
      setLogs(e instanceof Error ? e.message : 'Failed to fetch logs')
    } finally {
      setLoading(false)
    }
  }, [deployment])

  useEffect(() => {
    if (deployment) load()
  }, [deployment, load])

  return (
    <Dialog open={deployment !== null} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[720px]">
        <DialogHeader>
          <DialogTitle>
            Logs — {deployment?.name}
            <button onClick={load} title="Refresh" style={refreshBtnStyle} disabled={loading}>
              <RefreshCw size={12} />
            </button>
          </DialogTitle>
        </DialogHeader>
        <pre
          style={{
            margin: 0,
            padding: 12,
            maxHeight: 420,
            overflow: 'auto',
            background: 'var(--code-bg)',
            color: 'var(--code-text)',
            borderRadius: 8,
            fontSize: 11.5,
            fontFamily: 'var(--font-mono)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
          }}
        >
          {loading ? 'Loading…' : logs}
        </pre>
      </DialogContent>
    </Dialog>
  )
}

function StatusPill({ status }: { status: string }) {
  const colors = STATUS_COLORS[status] ?? STATUS_COLORS.stopped
  return (
    <span
      style={{
        fontSize: 10.5,
        fontWeight: 600,
        padding: '2px 8px',
        borderRadius: 999,
        background: colors.bg,
        color: colors.text,
        textTransform: 'uppercase',
        letterSpacing: 0.4,
      }}
    >
      {status}
    </span>
  )
}

function IconButton({
  title,
  onClick,
  icon,
  danger,
}: {
  title: string
  onClick: () => void
  icon: React.ReactNode
  danger?: boolean
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      style={{
        width: 28,
        height: 28,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        border: '1px solid var(--border)',
        borderRadius: 6,
        background: 'var(--surface)',
        color: danger ? 'var(--badge-red-text, #cf222e)' : 'var(--text-dim)',
        cursor: 'pointer',
      }}
    >
      {icon}
    </button>
  )
}

function EmptyState({ onCreate }: { onCreate: () => void }) {
  return (
    <div
      style={{
        border: '1px dashed var(--border-strong)',
        borderRadius: 10,
        padding: '48px 20px',
        textAlign: 'center',
        color: 'var(--text-dim)',
      }}
    >
      <Rocket size={22} style={{ color: 'var(--text-faint)', marginBottom: 10 }} />
      <div style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>
        No deployments yet
      </div>
      <div style={{ fontSize: 12.5, marginBottom: 16 }}>
        Import an OpenAPI spec, then deploy it as a running MCP server.
      </div>
      <Button size="sm" onClick={onCreate}>
        <Plus size={14} style={{ marginRight: 5 }} /> New deployment
      </Button>
    </div>
  )
}

function Banner({ tone, children }: { tone: 'warn' | 'error'; children: React.ReactNode }) {
  return (
    <div
      style={{
        marginBottom: 14,
        padding: '8px 12px',
        borderRadius: 8,
        fontSize: 12.5,
        background:
          tone === 'error' ? 'var(--badge-red-bg, #ffebe9)' : 'var(--badge-yellow-bg, #fff8e1)',
        color:
          tone === 'error'
            ? 'var(--badge-red-text, #cf222e)'
            : 'var(--badge-yellow-text, #9a6700)',
      }}
    >
      {children}
    </div>
  )
}

const fieldLabelStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 5,
  fontSize: 11,
  fontWeight: 700,
  color: 'var(--text)',
  textTransform: 'uppercase',
  letterSpacing: 0.5,
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  height: 34,
  border: '1px solid var(--border)',
  borderRadius: 7,
  background: 'var(--input-bg)',
  color: 'var(--text)',
  fontSize: 13,
  fontFamily: 'inherit',
  fontWeight: 400,
  textTransform: 'none',
  letterSpacing: 0,
  outline: 'none',
  padding: '0 10px',
}

const refreshBtnStyle: React.CSSProperties = {
  marginLeft: 8,
  width: 22,
  height: 22,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  border: '1px solid var(--border)',
  borderRadius: 5,
  background: 'var(--surface)',
  color: 'var(--text-dim)',
  cursor: 'pointer',
  verticalAlign: 'middle',
}
