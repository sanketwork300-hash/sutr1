import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Boxes, Plus, Server } from 'lucide-react'
import { api, type CustomMcpIntegration } from '@/api/client'
import { useCatalogStore } from '@/stores/catalog'
import { AddCustomMcpDialog } from '@/components/connections/AddCustomMcpDialog'
import { relativeTime } from '@/lib/format'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrEmpty,
  SutrEndpoint,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrStatus,
  SutrTable,
  describeError,
  type Column,
} from '@/components/sutr'

interface RemoteRow {
  id: string
  name: string
  url: string
  auth: string
  connected: boolean
  added: string | null
}

/**
 * Both kinds of MCP server this instance deals with: the ones it generates and
 * runs for you, and the remote ones it connects out to. The aggregate endpoint
 * at the top is what an agent actually points at — everything registered here
 * is reachable through that single URL.
 */
export default function McpServersPage() {
  const navigate = useNavigate()
  const { deployments, integrations, installed, loaded, load, refresh } = useCatalogStore()
  const [remote, setRemote] = useState<CustomMcpIntegration[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    api.customMcp
      .list()
      .then(setRemote)
      .catch((err) => setError(describeError(err).message))
  }, [])

  const aggregateUrl = `${window.location.origin}/mcp`
  const bundledRemote = integrations.filter(
    (integration) =>
      integration.type === 'remote_mcp' &&
      !(remote ?? []).some((custom) => custom.integration_id === integration.id),
  )

  const deploymentColumns: Column<(typeof deployments)[number]> = {
    key: 'name',
    header: 'Server',
    render: (deployment) => (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
        <span className="sutr-table__primary">{deployment.name}</span>
        <span className="sutr-meta sutr-mono">{deployment.slug}</span>
      </div>
    ),
  }

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="MCP servers"
        subtitle="Generated servers running on your infrastructure, and remote servers this instance connects out to."
        actions={
          <>
            <SutrButton variant="secondary" size="sm" onClick={() => setAddOpen(true)}>
              <Plus size={13} /> Add remote server
            </SutrButton>
            <SutrButton variant="brand" size="sm" onClick={() => navigate('/app/apis/new')}>
              Generate from an API
            </SutrButton>
          </>
        }
      />

      <SutrPageBody>
        <SutrCard>
          <SutrCardHeader
            title="This instance"
            meta="One endpoint aggregates every integration an agent is allowed to reach"
          />
          <SutrCardBody>
            <SutrEndpoint url={aggregateUrl} label="Aggregate MCP endpoint" />
            <p className="sutr-meta" style={{ marginTop: 10 }}>
              Streamable HTTP. Authenticate with an API key or the OAuth flow; the gateway applies
              each tool’s policy before the call leaves this instance.
            </p>
          </SutrCardBody>
        </SutrCard>

        {error ? <SutrError what="Remote MCP servers could not be listed." why={error} /> : null}

        <SutrCard>
          <SutrCardHeader
            title="Generated servers"
            meta="Compiled from an API specification and deployed as containers"
            actions={
              <SutrButton variant="ghost" size="sm" onClick={() => navigate('/app/deployments')}>
                Deployments
              </SutrButton>
            }
          />
          <SutrCardBody>
            {!loaded ? (
              <span className="sutr-skeleton" style={{ height: 48 }} />
            ) : (
              <SutrTable
                minWidth={560}
                columns={[
                  deploymentColumns,
                  {
                    key: 'provider',
                    header: 'Provider',
                    width: 120,
                    render: (deployment) => (
                      <span className="sutr-mono">{deployment.provider}</span>
                    ),
                  },
                  {
                    key: 'status',
                    header: 'Status',
                    width: 130,
                    render: (deployment) => (
                      <SutrStatus domain="deployment" value={deployment.status} />
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
                        <span
                          className="sutr-mono sutr-truncate"
                          style={{ maxWidth: 260, display: 'block' }}
                        >
                          {deployment.url}
                        </span>
                      ) : (
                        <span className="sutr-meta">not exposed yet</span>
                      ),
                  },
                ]}
                rows={deployments}
                rowKey={(deployment) => deployment.id}
                onRowClick={(deployment) =>
                  navigate(`/app/deployments?deployment=${encodeURIComponent(deployment.id)}`)
                }
                empty={
                  <SutrEmpty
                    icon={<Boxes size={17} />}
                    title="No generated servers"
                    body="Compile an OpenAPI specification into tools, then deploy it as a standalone MCP server on your own host or in your own cloud account."
                    action={
                      <SutrButton
                        variant="brand"
                        size="sm"
                        onClick={() => navigate('/app/apis/new')}
                      >
                        Generate a server
                      </SutrButton>
                    }
                  />
                }
              />
            )}
          </SutrCardBody>
        </SutrCard>

        <SutrCard>
          <SutrCardHeader
            title="Remote servers"
            meta="MCP servers hosted elsewhere that this instance proxies for your agents"
          />
          <SutrCardBody>
            {remote === null ? (
              <span className="sutr-skeleton" style={{ height: 48 }} />
            ) : remote.length === 0 && bundledRemote.length === 0 ? (
              <SutrEmpty
                icon={<Server size={17} />}
                title="No remote servers registered"
                body="Point Sutr at any MCP server you already run, or connect one of the bundled providers. Its tools then appear in the same catalog, under the same policies."
                action={
                  <SutrButton variant="secondary" size="sm" onClick={() => setAddOpen(true)}>
                    Add a remote server
                  </SutrButton>
                }
              />
            ) : (
              <SutrTable<RemoteRow>
                minWidth={560}
                columns={[
                  {
                    key: 'name',
                    header: 'Server',
                    render: (row) => (
                      <div
                        style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}
                      >
                        <span className="sutr-table__primary">{row.name}</span>
                        <span
                          className="sutr-meta sutr-mono sutr-truncate"
                          style={{ maxWidth: 380 }}
                        >
                          {row.url}
                        </span>
                      </div>
                    ),
                  },
                  {
                    key: 'auth',
                    header: 'Authentication',
                    width: 150,
                    render: (row) => (
                      <SutrBadge tone="neutral" plain>
                        {row.auth}
                      </SutrBadge>
                    ),
                  },
                  {
                    key: 'status',
                    header: 'Connection',
                    width: 150,
                    render: (row) => (
                      <SutrStatus
                        domain="connection"
                        value={row.connected ? 'connected' : 'disconnected'}
                      />
                    ),
                  },
                  {
                    key: 'added',
                    header: 'Added',
                    width: 130,
                    render: (row) => (
                      <span className="sutr-meta">{row.added ? relativeTime(row.added) : '—'}</span>
                    ),
                  },
                ]}
                rows={[
                  ...remote.map((server) => ({
                    id: server.integration_id,
                    name: server.name,
                    url: server.url,
                    auth: server.auth_method,
                    connected: installed.some(
                      (i) => i.integration_id === server.integration_id && i.connected,
                    ),
                    added: server.created_at,
                  })),
                  ...bundledRemote.map((integration) => ({
                    id: integration.id,
                    name: integration.name,
                    url: integration.url ?? '—',
                    auth: integration.auth[0]?.method ?? 'none',
                    connected: installed.some(
                      (i) => i.integration_id === integration.id && i.connected,
                    ),
                    added: null as string | null,
                  })),
                ]}
                rowKey={(row) => row.id}
                onRowClick={(row) => navigate(`/app/integrations/${encodeURIComponent(row.id)}`)}
              />
            )}
          </SutrCardBody>
        </SutrCard>
      </SutrPageBody>

      <AddCustomMcpDialog
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onCreated={() => {
          setAddOpen(false)
          api.customMcp
            .list()
            .then(setRemote)
            .catch(() => undefined)
          void refresh()
        }}
      />
    </SutrPage>
  )
}
