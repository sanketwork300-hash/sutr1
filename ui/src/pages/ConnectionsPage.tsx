import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Plug, Plus } from 'lucide-react'
import { useConnectionsStore } from '@/stores/connections'
import { useCatalogStore } from '@/stores/catalog'
import { ConnectDialog } from '@/components/connections/ConnectDialog'
import { AddCustomMcpDialog } from '@/components/connections/AddCustomMcpDialog'
import { NewIntegrationChooser } from '@/components/connections/NewIntegrationChooser'
import { api, type BundledIntegration, type LogEntry } from '@/api/client'
import { relativeTime } from '@/lib/format'
import {
  SutrBadge,
  SutrButton,
  SutrEmpty,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSearchInput,
  SutrSegmented,
  SutrStatus,
  SutrTable,
  type Column,
} from '@/components/sutr'

type Filter = 'all' | 'connected' | 'available' | 'unavailable'

interface Row {
  integration: BundledIntegration
  connected: boolean
  toolCount: number
  destructive: number
  lastUsed: string | null
}

const TYPE_LABEL: Record<string, string> = {
  remote_mcp: 'Remote MCP',
  custom: 'REST API',
}

function authSummary(integration: BundledIntegration): string {
  const methods = [...new Set(integration.auth.map((a) => a.method))]
  if (methods.length === 0) return 'none'
  return methods.join(' · ')
}

/**
 * A capability catalog, not a wall of logos. Each row answers the questions an
 * operator actually has: how it authenticates, how many tools it exposes, how
 * many of those can change something, whether it is connected, and when an
 * agent last used it.
 */
export default function ConnectionsPage() {
  const navigate = useNavigate()
  const {
    integrations,
    installed,
    fetchIntegrations,
    fetchInstalled,
    fetchCustomMcp,
    fetchCustomApi,
  } = useConnectionsStore()
  const tools = useCatalogStore((s) => s.tools)
  const loadCatalog = useCatalogStore((s) => s.load)

  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [connectTarget, setConnectTarget] = useState<BundledIntegration | null>(null)
  const [addCustomOpen, setAddCustomOpen] = useState(false)
  const [chooserOpen, setChooserOpen] = useState(false)
  const [recent, setRecent] = useState<LogEntry[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([fetchIntegrations(), fetchInstalled(), fetchCustomMcp(), fetchCustomApi()])
      .catch(() => undefined)
      .finally(() => setLoading(false))
    void loadCatalog()
    // "Last used" is derived from the execution log rather than stored on the
    // integration, so it is always the truth and never a cached guess.
    api.logs
      .list({ limit: 200 })
      .then(setRecent)
      .catch(() => setRecent([]))
  }, [fetchIntegrations, fetchInstalled, fetchCustomMcp, fetchCustomApi, loadCatalog])

  const rows = useMemo<Row[]>(() => {
    const connectedIds = new Set(installed.filter((i) => i.connected).map((i) => i.integration_id))
    const lastUsed = new Map<string, string>()
    for (const entry of recent) {
      if (!lastUsed.has(entry.integration_id)) lastUsed.set(entry.integration_id, entry.timestamp)
    }
    return integrations.map((integration) => {
      const own = tools.filter((tool) => tool.integration_id === integration.id)
      return {
        integration,
        connected: connectedIds.has(integration.id),
        toolCount: own.length || integration.tools?.length || 0,
        destructive: own.filter((tool) => tool.annotations?.destructiveHint).length,
        lastUsed: lastUsed.get(integration.id) ?? null,
      }
    })
  }, [integrations, installed, tools, recent])

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return rows
      .filter((row) => {
        if (filter === 'connected' && !row.connected) return false
        if (filter === 'available' && (row.connected || row.integration.available === false)) {
          return false
        }
        if (filter === 'unavailable' && row.integration.available !== false) return false
        if (!needle) return true
        return (
          row.integration.name.toLowerCase().includes(needle) ||
          row.integration.id.toLowerCase().includes(needle) ||
          (row.integration.description ?? '').toLowerCase().includes(needle)
        )
      })
      .sort((a, b) => {
        if (a.connected !== b.connected) return a.connected ? -1 : 1
        return a.integration.name.localeCompare(b.integration.name)
      })
  }, [rows, filter, search])

  const columns: Column<Row>[] = [
    {
      key: 'provider',
      header: 'Provider',
      render: (row) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          <span className="sutr-table__primary">{row.integration.name}</span>
          <span className="sutr-meta sutr-truncate" style={{ maxWidth: 420 }}>
            {row.integration.description}
          </span>
        </div>
      ),
    },
    {
      key: 'type',
      header: 'Type',
      width: 120,
      render: (row) => (
        <SutrBadge tone="neutral" plain>
          {TYPE_LABEL[row.integration.type] ?? row.integration.type}
        </SutrBadge>
      ),
    },
    {
      key: 'auth',
      header: 'Authentication',
      width: 140,
      render: (row) => <span className="sutr-mono">{authSummary(row.integration)}</span>,
    },
    {
      key: 'tools',
      header: 'Tools',
      numeric: true,
      width: 80,
      render: (row) => (row.toolCount > 0 ? row.toolCount : <span className="sutr-meta">—</span>),
    },
    {
      key: 'risk',
      header: 'Risk',
      width: 130,
      render: (row) =>
        !row.connected ? (
          <span className="sutr-meta">not connected</span>
        ) : row.destructive > 0 ? (
          <SutrBadge tone="warning" plain title="Tools annotated as destructive by the provider">
            {row.destructive} destructive
          </SutrBadge>
        ) : (
          <SutrBadge tone="neutral" plain>
            no destructive tools
          </SutrBadge>
        ),
    },
    {
      key: 'status',
      header: 'Connection',
      width: 170,
      render: (row) => (
        <SutrStatus
          domain="connection"
          value={
            row.integration.available === false
              ? 'unavailable'
              : row.connected
                ? 'connected'
                : 'not_configured'
          }
          title={row.integration.available_reason ?? undefined}
        />
      ),
    },
    {
      key: 'lastUsed',
      header: 'Last used',
      width: 130,
      render: (row) =>
        row.lastUsed ? (
          <span className="sutr-meta">{relativeTime(row.lastUsed)}</span>
        ) : (
          <span className="sutr-meta">never</span>
        ),
    },
    {
      key: 'actions',
      header: '',
      width: 120,
      render: (row) => (
        <div className="sutr-table__actions">
          {row.connected ? (
            <SutrButton
              variant="ghost"
              size="sm"
              onClick={(e) => {
                e.stopPropagation()
                navigate(`/app/integrations/${encodeURIComponent(row.integration.id)}`)
              }}
            >
              Manage
            </SutrButton>
          ) : row.integration.available === false ? (
            <span className="sutr-meta" title={row.integration.available_reason ?? undefined}>
              unavailable
            </span>
          ) : (
            <SutrButton
              variant="secondary"
              size="sm"
              onClick={(e) => {
                e.stopPropagation()
                setConnectTarget(row.integration)
              }}
            >
              Connect
            </SutrButton>
          )}
        </div>
      ),
    },
  ]

  const counts = {
    all: rows.length,
    connected: rows.filter((r) => r.connected).length,
    available: rows.filter((r) => !r.connected && r.integration.available !== false).length,
    unavailable: rows.filter((r) => r.integration.available === false).length,
  }

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="Integrations"
        subtitle="Every provider this instance can reach, what it authenticates with, and what it exposes to agents."
        actions={
          <SutrButton variant="brand" size="sm" onClick={() => setChooserOpen(true)}>
            <Plus size={13} /> New integration
          </SutrButton>
        }
      />

      <SutrPageBody>
        <div className="sutr-page__toolbar">
          <SutrSearchInput
            value={search}
            onValueChange={setSearch}
            ariaLabel="Search integrations"
            placeholder="Search providers"
            maxWidth={320}
          />
          <SutrSegmented
            ariaLabel="Filter integrations"
            value={filter}
            onChange={setFilter}
            items={[
              { value: 'all', label: `All ${counts.all}` },
              { value: 'connected', label: `Connected ${counts.connected}` },
              { value: 'available', label: `Available ${counts.available}` },
              ...(counts.unavailable > 0
                ? [{ value: 'unavailable' as Filter, label: `Unavailable ${counts.unavailable}` }]
                : []),
            ]}
          />
        </div>

        {loading ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {[0, 1, 2, 3, 4].map((i) => (
              <span key={i} className="sutr-skeleton" style={{ height: 46 }} />
            ))}
          </div>
        ) : (
          <SutrTable
            columns={columns}
            rows={visible}
            minWidth={960}
            rowKey={(row) => row.integration.id}
            onRowClick={(row) =>
              row.integration.available === false
                ? undefined
                : navigate(`/app/integrations/${encodeURIComponent(row.integration.id)}`)
            }
            caption="Available integrations"
            empty={
              <SutrEmpty
                icon={<Plug size={17} />}
                title={integrations.length === 0 ? 'No integrations available' : 'Nothing matches'}
                body={
                  integrations.length === 0
                    ? 'Add a remote MCP server, or compile an OpenAPI specification into a governed integration of your own.'
                    : 'Clear the search or choose a different filter.'
                }
                action={
                  <SutrButton variant="brand" size="sm" onClick={() => setChooserOpen(true)}>
                    New integration
                  </SutrButton>
                }
              />
            }
          />
        )}
      </SutrPageBody>

      <ConnectDialog
        integration={connectTarget}
        open={connectTarget !== null}
        onClose={() => setConnectTarget(null)}
      />

      <AddCustomMcpDialog
        open={addCustomOpen}
        onClose={() => setAddCustomOpen(false)}
        onCreated={(created) => {
          // Surface the new integration immediately by opening the Connect
          // dialog; the catalog refresh behind it fills in the row.
          fetchIntegrations().then(() => {
            const fresh = useConnectionsStore
              .getState()
              .integrations.find((i) => i.id === created.integration_id)
            if (fresh) setConnectTarget(fresh)
          })
        }}
      />

      <NewIntegrationChooser
        open={chooserOpen}
        onClose={() => setChooserOpen(false)}
        onPickMcp={() => {
          setChooserOpen(false)
          setAddCustomOpen(true)
        }}
        onPickOpenApi={() => {
          setChooserOpen(false)
          navigate('/app/apis/new')
        }}
      />
    </SutrPage>
  )
}
