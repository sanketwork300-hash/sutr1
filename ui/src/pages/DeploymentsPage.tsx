import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { ExternalLink, FileText, Play, Plus, RefreshCw, Rocket, Square, Trash2 } from 'lucide-react'
import {
  api,
  type Deployment,
  type DeploymentProviderInfo,
  type OpenApiProject,
} from '@/api/client'
import { DeployStep } from '@/components/mcp-builder/DeployStep'
import { formatDateTime, relativeTime } from '@/lib/format'
import {
  SutrBadge,
  SutrButton,
  SutrCodeBlock,
  SutrDefinitionList,
  SutrDrawer,
  SutrEmpty,
  SutrEndpoint,
  SutrError,
  SutrExternalButton,
  SutrModal,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSectionLabel,
  SutrSelect,
  SutrStatus,
  SutrTable,
  describeError,
  type Column,
} from '@/components/sutr'

const TRANSITIONAL = new Set(['queued', 'building', 'deploying'])

/**
 * The infrastructure view of generated MCP servers: what is running, where,
 * and on whose authority. Status is whatever the provider last reported —
 * "RUNNING" is never inferred from a successful create call.
 */
export default function DeploymentsPage() {
  const [params, setParams] = useSearchParams()
  const [deployments, setDeployments] = useState<Deployment[] | null>(null)
  const [providers, setProviders] = useState<DeploymentProviderInfo[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [logsFor, setLogsFor] = useState<Deployment | null>(null)
  const [logs, setLogs] = useState<string>('')
  const [logsLoading, setLogsLoading] = useState(false)
  const pollRef = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      const rows = await api.deployments.list()
      setDeployments(rows)
      setError(null)
      return rows
    } catch (err) {
      setError(describeError(err).message)
      setDeployments([])
      return []
    }
  }, [])

  useEffect(() => {
    void refresh()
    api.deployments
      .providers()
      .then(setProviders)
      .catch(() => setProviders([]))
  }, [refresh])

  // Poll while anything is mid-transition so the status changes on its own.
  useEffect(() => {
    const moving = (deployments ?? []).some((d) => TRANSITIONAL.has(d.status))
    if (moving && pollRef.current === null) {
      pollRef.current = window.setInterval(() => void refresh(), 3000)
    }
    if (!moving && pollRef.current !== null) {
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

  const selectedId = params.get('deployment')
  const selected = (deployments ?? []).find((d) => d.id === selectedId) ?? null

  function openDetail(deployment: Deployment) {
    params.set('deployment', deployment.id)
    setParams(params, { replace: true })
  }

  function closeDetail() {
    if (params.get('deployment')) {
      params.delete('deployment')
      setParams(params, { replace: true })
    }
  }

  async function act(deployment: Deployment, action: 'start' | 'stop' | 'delete') {
    setBusy(deployment.id)
    setError(null)
    try {
      if (action === 'delete') {
        await api.deployments.remove(deployment.id)
        closeDetail()
      } else {
        await api.deployments[action](deployment.id)
      }
      await refresh()
    } catch (err) {
      setError(describeError(err).message)
    } finally {
      setBusy(null)
    }
  }

  const loadLogs = useCallback(async (deployment: Deployment) => {
    setLogsLoading(true)
    try {
      const result = await api.deployments.logs(deployment.id)
      setLogs(result.logs || '(the container has produced no output yet)')
    } catch (err) {
      setLogs(describeError(err).message)
    } finally {
      setLogsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (logsFor) void loadLogs(logsFor)
  }, [logsFor, loadLogs])

  const localProvider = providers.find((p) => p.id === 'docker')

  const columns: Column<Deployment>[] = [
    {
      key: 'name',
      header: 'Deployment',
      render: (deployment) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          <span className="sutr-table__primary">{deployment.name}</span>
          <span className="sutr-meta sutr-mono">{deployment.slug}</span>
        </div>
      ),
    },
    {
      key: 'provider',
      header: 'Provider',
      width: 120,
      render: (deployment) => <span className="sutr-mono">{deployment.provider}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      width: 140,
      render: (deployment) => (
        <SutrStatus
          domain="deployment"
          value={deployment.status}
          title={deployment.error ?? undefined}
        />
      ),
    },
    {
      key: 'tools',
      header: 'Tools',
      numeric: true,
      width: 80,
      render: (deployment) => deployment.tool_count,
    },
    {
      key: 'endpoint',
      header: 'Endpoint',
      render: (deployment) =>
        deployment.url ? (
          <span className="sutr-mono sutr-truncate" style={{ maxWidth: 280, display: 'block' }}>
            {deployment.url}
          </span>
        ) : deployment.status === 'failed' ? (
          <span className="sutr-meta" style={{ color: 'var(--red)' }}>
            {deployment.error ?? 'failed'}
          </span>
        ) : (
          <span className="sutr-meta">not exposed yet</span>
        ),
    },
    {
      key: 'created',
      header: 'Created',
      width: 130,
      render: (deployment) => (
        <span className="sutr-meta" title={formatDateTime(deployment.created_at)}>
          {relativeTime(deployment.created_at)}
        </span>
      ),
    },
    {
      key: 'actions',
      header: '',
      width: 130,
      render: (deployment) => (
        <div className="sutr-table__actions">
          <SutrButton
            variant="ghost"
            size="sm"
            iconOnly
            title="Logs"
            aria-label={`Logs for ${deployment.name}`}
            onClick={(e) => {
              e.stopPropagation()
              setLogsFor(deployment)
            }}
          >
            <FileText size={13} />
          </SutrButton>
          {deployment.status === 'running' ? (
            <SutrButton
              variant="ghost"
              size="sm"
              iconOnly
              title="Stop"
              aria-label={`Stop ${deployment.name}`}
              loading={busy === deployment.id}
              onClick={(e) => {
                e.stopPropagation()
                void act(deployment, 'stop')
              }}
            >
              <Square size={13} />
            </SutrButton>
          ) : deployment.status === 'stopped' ? (
            <SutrButton
              variant="ghost"
              size="sm"
              iconOnly
              title="Start"
              aria-label={`Start ${deployment.name}`}
              loading={busy === deployment.id}
              onClick={(e) => {
                e.stopPropagation()
                void act(deployment, 'start')
              }}
            >
              <Play size={13} />
            </SutrButton>
          ) : null}
        </div>
      ),
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Operate"
        title="Deployments"
        subtitle="Generated MCP servers, the provider each one runs on, and the endpoint agents reach it at."
        actions={
          <>
            <SutrButton variant="secondary" size="sm" onClick={() => void refresh()}>
              <RefreshCw size={13} /> Refresh
            </SutrButton>
            <SutrButton variant="brand" size="sm" onClick={() => setCreateOpen(true)}>
              <Plus size={13} /> New deployment
            </SutrButton>
          </>
        }
      />

      <SutrPageBody>
        {localProvider && !localProvider.enabled && localProvider.reason ? (
          <SutrError
            what="The local Docker provider is not available on this instance."
            why={localProvider.reason}
            meta={{ provider: 'docker' }}
          />
        ) : null}

        {error ? <SutrError what="That deployment action did not complete." why={error} /> : null}

        {deployments === null ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {[0, 1, 2].map((i) => (
              <span key={i} className="sutr-skeleton" style={{ height: 46 }} />
            ))}
          </div>
        ) : (
          <SutrTable
            columns={columns}
            rows={deployments}
            minWidth={940}
            rowKey={(deployment) => deployment.id}
            onRowClick={openDetail}
            caption="Deployments"
            empty={
              <SutrEmpty
                icon={<Rocket size={17} />}
                title="No deployments yet"
                body="Compile an OpenAPI specification into tools, then run it as a standalone MCP server — on this host, or in your own cloud account."
                action={
                  <SutrButton variant="brand" size="sm" onClick={() => setCreateOpen(true)}>
                    Create the first deployment
                  </SutrButton>
                }
              />
            }
          />
        )}
      </SutrPageBody>

      <DeploymentDetail
        deployment={selected}
        busy={busy === selected?.id}
        onClose={closeDetail}
        onLogs={() => selected && setLogsFor(selected)}
        onAct={(action) => selected && act(selected, action)}
      />

      <NewDeploymentModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onDeployed={() => {
          setCreateOpen(false)
          void refresh()
        }}
      />

      <SutrModal
        open={Boolean(logsFor)}
        onClose={() => setLogsFor(null)}
        wide
        title={`Logs — ${logsFor?.name ?? ''}`}
        description="Container output as the provider reports it."
        footer={
          <SutrButton
            variant="secondary"
            loading={logsLoading}
            onClick={() => logsFor && void loadLogs(logsFor)}
          >
            <RefreshCw size={13} /> Refresh
          </SutrButton>
        }
      >
        <SutrCodeBlock
          label="stdout / stderr"
          copyable={false}
          maxHeight={420}
          code={logsLoading ? 'Reading logs…' : logs}
        />
      </SutrModal>
    </SutrPage>
  )
}

function DeploymentDetail({
  deployment,
  busy,
  onClose,
  onLogs,
  onAct,
}: {
  deployment: Deployment | null
  busy: boolean
  onClose: () => void
  onLogs: () => void
  onAct: (action: 'start' | 'stop' | 'delete') => void
}) {
  if (!deployment) return null

  const config = Object.entries(deployment.config ?? {})

  return (
    <SutrDrawer
      open
      onClose={onClose}
      wide
      title={deployment.name}
      subtitle={`${deployment.provider} · ${deployment.tool_count} tool${deployment.tool_count === 1 ? '' : 's'}`}
      actions={<SutrStatus domain="deployment" value={deployment.status} />}
      footer={
        <>
          <SutrButton
            variant="danger"
            size="sm"
            loading={busy}
            onClick={() => {
              if (
                window.confirm(
                  `Delete deployment “${deployment.name}”? This tears down the running container.`,
                )
              ) {
                onAct('delete')
              }
            }}
          >
            <Trash2 size={13} /> Delete
          </SutrButton>
          <span style={{ flex: 1 }} />
          <SutrButton variant="secondary" size="sm" onClick={onLogs}>
            <FileText size={13} /> View logs
          </SutrButton>
          {deployment.status === 'running' ? (
            <SutrButton variant="secondary" size="sm" loading={busy} onClick={() => onAct('stop')}>
              <Square size={13} /> Stop
            </SutrButton>
          ) : deployment.status === 'stopped' ? (
            <SutrButton variant="brand" size="sm" loading={busy} onClick={() => onAct('start')}>
              <Play size={13} /> Start
            </SutrButton>
          ) : null}
        </>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
        {deployment.status === 'failed' && deployment.error ? (
          <SutrError
            what="This deployment failed."
            why={deployment.error}
            meta={{ provider: deployment.provider, deployment: deployment.id }}
            action={
              <SutrButton variant="secondary" size="sm" onClick={onLogs}>
                View logs
              </SutrButton>
            }
          />
        ) : null}

        {deployment.url ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <SutrEndpoint url={deployment.url} label="MCP endpoint" />
            <div style={{ display: 'flex', gap: 8 }}>
              <SutrExternalButton href={deployment.url} variant="brand" size="sm">
                Open MCP endpoint <ExternalLink size={12} />
              </SutrExternalButton>
              {deployment.console_url ? (
                <SutrExternalButton href={deployment.console_url} variant="secondary" size="sm">
                  Provider console <ExternalLink size={12} />
                </SutrExternalButton>
              ) : null}
            </div>
          </div>
        ) : (
          <p className="sutr-meta">
            No endpoint yet. One appears once the provider reports the service as reachable.
          </p>
        )}

        <SutrDefinitionList
          items={[
            { key: 'Deployment ID', value: <code className="sutr-mono">{deployment.id}</code> },
            { key: 'Slug', value: <code className="sutr-mono">{deployment.slug}</code> },
            { key: 'Provider', value: <code className="sutr-mono">{deployment.provider}</code> },
            {
              key: 'Health check',
              value: deployment.health_url ? (
                <code className="sutr-mono" style={{ overflowWrap: 'anywhere' }}>
                  {deployment.health_url}
                </code>
              ) : (
                <span className="sutr-meta">not published by this provider</span>
              ),
            },
            {
              key: 'Runtime token',
              value: deployment.has_token ? (
                <SutrBadge tone="success" plain>
                  injected as {deployment.env_var ?? 'an environment variable'}
                </SutrBadge>
              ) : (
                <span className="sutr-meta">none — the upstream needs no credential</span>
              ),
            },
            { key: 'Created', value: formatDateTime(deployment.created_at) },
            {
              key: 'Updated',
              value: `${formatDateTime(deployment.updated_at)} (${relativeTime(deployment.updated_at)})`,
            },
          ]}
        />

        {config.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <SutrSectionLabel>Placement</SutrSectionLabel>
            <SutrDefinitionList
              items={config.map(([key, value]) => ({
                key,
                value: <code className="sutr-mono">{value}</code>,
              }))}
            />
          </div>
        ) : null}
      </div>
    </SutrDrawer>
  )
}

/**
 * Deploying from here reuses the builder's deploy stage, so the provider list,
 * the fields each provider needs and the credential flow are identical — and
 * still come from the server's provider registry.
 */
function NewDeploymentModal({
  open,
  onClose,
  onDeployed,
}: {
  open: boolean
  onClose: () => void
  onDeployed: () => void
}) {
  const [projects, setProjects] = useState<OpenApiProject[] | null>(null)
  const [projectId, setProjectId] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    api.openapi
      .list()
      .then((rows) => {
        setProjects(rows)
        if (rows.length > 0) setProjectId((current) => current || rows[0].id)
      })
      .catch((err) => setError(describeError(err).message))
  }, [open])

  const project = (projects ?? []).find((p) => p.id === projectId) ?? null

  return (
    <SutrModal
      open={open}
      onClose={onClose}
      wide
      title="New deployment"
      description="Choose an imported specification, then pick where it runs. Every operation in the specification is compiled unless you narrow the selection in the builder."
    >
      {error ? <SutrError what="Specifications could not be listed." why={error} /> : null}

      {projects !== null && projects.length === 0 ? (
        <SutrEmpty
          title="No specifications imported"
          body="A deployment is built from an OpenAPI project. Import one first — the builder walks through it stage by stage."
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
            <span className="sutr-field__label">Specification</span>
            <SutrSelect value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              {(projects ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.api_title || p.name} · {p.operation_count} operations
                </option>
              ))}
            </SutrSelect>
          </label>

          {project ? (
            <DeployStep
              project={project}
              compileBody={() => ({})}
              suggestedName={project.api_title || project.name}
              onBack={onClose}
              onOpenDeployments={onDeployed}
            />
          ) : null}
        </div>
      )}
    </SutrModal>
  )
}
