import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { FlaskConical, RefreshCw, Wrench } from 'lucide-react'
import { api, type LogEntry, type Tool } from '@/api/client'
import { useCatalogStore } from '@/stores/catalog'
import { isTotpChallengeError } from '@/lib/totpError'
import { formatClock, formatDuration, relativeTime } from '@/lib/format'
import { TotpCodeDialog } from '@/components/totp/TotpCodeDialog'
import {
  SutrBadge,
  SutrButton,
  SutrDefinitionList,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrCodeBlock,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSearchInput,
  SutrSegmented,
  SutrSelect,
  SutrSectionLabel,
  SutrStatus,
  SutrTable,
  SutrTimeline,
  describeError,
  type Column,
  type TimelineEntry,
} from '@/components/sutr'

type ModeFilter = 'all' | 'allow' | 'require_approval' | 'deny'

const MODES = [
  { value: 'allow', label: 'Auto approve' },
  { value: 'require_approval', label: 'Ask' },
  { value: 'deny', label: 'Deny' },
]

/** Risk from the tool's own MCP annotations — the only claim the protocol
 *  makes about what a tool does. Unannotated tools say so rather than guess. */
function riskOf(tool: Tool): { label: string; tone: 'neutral' | 'warning' | 'danger' } {
  if (tool.annotations?.destructiveHint) return { label: 'destructive', tone: 'danger' }
  if (tool.annotations?.readOnlyHint) return { label: 'read only', tone: 'neutral' }
  return { label: 'unannotated', tone: 'warning' }
}

export default function ToolsPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const { tools, loaded, loading, error, load, refresh } = useCatalogStore()

  const [query, setQuery] = useState('')
  const [integration, setIntegration] = useState('all')
  const [mode, setMode] = useState<ModeFilter>('all')
  const [policyError, setPolicyError] = useState<string | null>(null)
  const [totpOpen, setTotpOpen] = useState(false)
  const [pendingMode, setPendingMode] = useState<string | null>(null)

  // The inspector's target is a key, not a snapshot: the row it points at is
  // re-derived from the catalog, so a policy change is reflected immediately
  // and a deep link from the command palette works before the catalog loads.
  const [selectedKey, setSelectedKey] = useState<string | null>(() => {
    const toolName = params.get('tool')
    const integrationId = params.get('integration')
    return toolName ? `${integrationId ?? ''}:${toolName}` : null
  })
  const [executions, setExecutions] = useState<{ key: string; rows: LogEntry[] } | null>(null)

  useEffect(() => {
    void load()
  }, [load])

  const selected = useMemo(() => {
    if (!selectedKey) return null
    const [integrationId, name] = [
      selectedKey.slice(0, selectedKey.indexOf(':')),
      selectedKey.slice(selectedKey.indexOf(':') + 1),
    ]
    return (
      tools.find(
        (tool) => tool.name === name && (!integrationId || tool.integration_id === integrationId),
      ) ?? null
    )
  }, [selectedKey, tools])

  useEffect(() => {
    if (!selected?.integration_id) return
    const key = `${selected.integration_id}:${selected.name}`
    let cancelled = false
    api.logs
      .list({ integration: selected.integration_id, tool: selected.name, limit: 8 })
      .then((rows) => {
        if (!cancelled) setExecutions({ key, rows })
      })
      .catch(() => {
        if (!cancelled) setExecutions({ key, rows: [] })
      })
    return () => {
      cancelled = true
    }
  }, [selected])

  const executionRows =
    selected && executions?.key === `${selected.integration_id}:${selected.name}`
      ? executions.rows
      : null

  const integrations = useMemo(
    () => [...new Set(tools.map((t) => t.integration_id).filter(Boolean))].sort() as string[],
    [tools],
  )

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return tools.filter((tool) => {
      if (integration !== 'all' && tool.integration_id !== integration) return false
      if (mode !== 'all' && (tool.execution_mode ?? 'allow') !== mode) return false
      if (!needle) return true
      return (
        tool.name.toLowerCase().includes(needle) ||
        (tool.description ?? '').toLowerCase().includes(needle)
      )
    })
  }, [tools, query, integration, mode])

  async function applyMode(next: string, totpCode?: string) {
    if (!selected?.integration_id) return
    setPolicyError(null)
    try {
      await api.toolSettings.update(selected.integration_id, selected.name, next, totpCode)
      // The catalog is the source of truth for the mode, so re-read it rather
      // than patching a local copy that could drift from the server.
      await refresh()
      setPendingMode(null)
    } catch (err) {
      if (isTotpChallengeError(err)) {
        setPendingMode(next)
        setTotpOpen(true)
        return
      }
      setPolicyError(describeError(err).message)
    }
  }

  const columns: Column<Tool>[] = [
    {
      key: 'name',
      header: 'Tool',
      render: (tool) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          <code className="sutr-mono sutr-table__primary">{tool.name}</code>
          {tool.description ? (
            <span className="sutr-meta sutr-truncate" style={{ maxWidth: 420 }}>
              {tool.description}
            </span>
          ) : null}
        </div>
      ),
    },
    {
      key: 'integration',
      header: 'Provider',
      width: 150,
      render: (tool) => <span className="sutr-mono">{tool.integration_id}</span>,
    },
    {
      key: 'policy',
      header: 'Policy',
      width: 150,
      render: (tool) => <SutrStatus domain="mode" value={tool.execution_mode ?? 'allow'} />,
    },
    {
      key: 'risk',
      header: 'Risk',
      width: 130,
      render: (tool) => {
        const risk = riskOf(tool)
        return (
          <SutrBadge tone={risk.tone} plain>
            {risk.label}
          </SutrBadge>
        )
      },
    },
    {
      key: 'actions',
      header: '',
      width: 96,
      render: (tool) => (
        <div className="sutr-table__actions">
          <SutrButton
            variant="ghost"
            size="sm"
            onClick={(e) => {
              e.stopPropagation()
              navigate(
                `/app/playground?integration=${encodeURIComponent(
                  tool.integration_id ?? '',
                )}&tool=${encodeURIComponent(tool.name)}`,
              )
            }}
          >
            Run
          </SutrButton>
        </div>
      ),
    },
  ]

  const trace: TimelineEntry[] = (executionRows ?? []).map((entry) => ({
    id: String(entry.id),
    time: formatClock(entry.timestamp),
    tone:
      entry.outcome === 'denied' || entry.outcome === 'error'
        ? 'danger'
        : entry.outcome === 'approval_required'
          ? 'warning'
          : 'success',
    title: <SutrStatus domain="outcome" value={entry.outcome} />,
    detail: `${formatDuration(entry.duration_ms)} · ${relativeTime(entry.timestamp)}`,
  }))

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="Tools"
        subtitle="Every capability an agent can call through this instance, with the policy that governs it."
        actions={
          <SutrButton
            variant="secondary"
            size="sm"
            onClick={() => void refresh()}
            loading={loading}
          >
            <RefreshCw size={13} /> Refresh
          </SutrButton>
        }
      />

      <SutrPageBody>
        <div className="sutr-page__toolbar">
          <SutrSearchInput
            value={query}
            onValueChange={setQuery}
            ariaLabel="Search tools"
            placeholder="Search by name or description"
            maxWidth={340}
          />
          <SutrSelect
            aria-label="Filter by provider"
            value={integration}
            onChange={(e) => setIntegration(e.target.value)}
            style={{ width: 190 }}
          >
            <option value="all">All providers</option>
            {integrations.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </SutrSelect>
          <SutrSegmented
            ariaLabel="Filter by policy"
            value={mode}
            onChange={setMode}
            items={[
              { value: 'all', label: 'All' },
              { value: 'allow', label: 'Auto' },
              { value: 'require_approval', label: 'Ask' },
              { value: 'deny', label: 'Deny' },
            ]}
          />
          <span className="sutr-meta" style={{ marginLeft: 'auto' }}>
            {rows.length} of {tools.length} tools
          </span>
        </div>

        {error ? <SutrError what="The tool catalog could not be read." why={error} /> : null}

        {!loaded ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {[0, 1, 2, 3, 4].map((i) => (
              <span key={i} className="sutr-skeleton" style={{ height: 44 }} />
            ))}
          </div>
        ) : (
          <SutrTable
            columns={columns}
            rows={rows}
            rowKey={(tool) => `${tool.integration_id}:${tool.name}`}
            onRowClick={(tool) => setSelectedKey(`${tool.integration_id ?? ''}:${tool.name}`)}
            caption="Tools available to agents"
            empty={
              <SutrEmpty
                icon={<Wrench size={17} />}
                title={tools.length === 0 ? 'No tools yet' : 'No tools match these filters'}
                body={
                  tools.length === 0
                    ? 'Connect an integration or compile an OpenAPI specification, and its operations appear here as governed tools.'
                    : 'Clear the search or widen the policy filter.'
                }
                action={
                  tools.length === 0 ? (
                    <SutrButton variant="brand" size="sm" onClick={() => navigate('/app/apis/new')}>
                      Import an API specification
                    </SutrButton>
                  ) : undefined
                }
              />
            }
          />
        )}
      </SutrPageBody>

      <SutrDrawer
        open={Boolean(selected)}
        onClose={() => {
          setSelectedKey(null)
          setPolicyError(null)
          if (params.get('tool')) {
            params.delete('tool')
            params.delete('integration')
            setParams(params, { replace: true })
          }
        }}
        title={selected ? <code className="sutr-mono">{selected.name}</code> : ''}
        subtitle={selected?.integration_id}
        wide
        footer={
          selected ? (
            <>
              <SutrButton
                variant="secondary"
                onClick={() =>
                  navigate(
                    `/app/activity?integration=${encodeURIComponent(
                      selected.integration_id ?? '',
                    )}&tool=${encodeURIComponent(selected.name)}`,
                  )
                }
              >
                View activity
              </SutrButton>
              <SutrButton
                variant="brand"
                onClick={() =>
                  navigate(
                    `/app/playground?integration=${encodeURIComponent(
                      selected.integration_id ?? '',
                    )}&tool=${encodeURIComponent(selected.name)}`,
                  )
                }
              >
                <FlaskConical size={13} /> Run tool
              </SutrButton>
            </>
          ) : null
        }
      >
        {selected ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
            {selected.description ? <p className="sutr-body">{selected.description}</p> : null}

            <SutrDefinitionList
              items={[
                {
                  key: 'Provider',
                  value: <code className="sutr-mono">{selected.integration_id}</code>,
                },
                { key: 'Title', value: selected.title ?? selected.name },
                {
                  key: 'Risk',
                  value: (
                    <SutrBadge tone={riskOf(selected).tone} plain>
                      {riskOf(selected).label}
                    </SutrBadge>
                  ),
                },
                {
                  key: 'Category',
                  value: selected.category ?? <span className="sutr-meta">none</span>,
                },
              ]}
            />

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <SutrSectionLabel>Approval policy</SutrSectionLabel>
              <SutrSegmented
                ariaLabel="Execution policy"
                value={selected.execution_mode ?? 'allow'}
                onChange={(next) => void applyMode(next)}
                items={MODES}
              />
              <span className="sutr-meta">
                Evaluated on every call. “Ask” pauses execution until a person approves the exact
                arguments; “Deny” refuses before the request reaches the provider.
              </span>
              {policyError ? (
                <SutrError what="The policy was not changed." why={policyError} />
              ) : null}
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <SutrSectionLabel>Input schema</SutrSectionLabel>
              <SutrCodeBlock
                label="json schema"
                maxHeight={280}
                code={JSON.stringify(selected.inputSchema ?? {}, null, 2)}
              />
            </div>

            {selected.outputSchema ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <SutrSectionLabel>Output schema</SutrSectionLabel>
                <SutrCodeBlock
                  label="json schema"
                  maxHeight={220}
                  code={JSON.stringify(selected.outputSchema, null, 2)}
                />
              </div>
            ) : null}

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <SutrSectionLabel>Recent executions</SutrSectionLabel>
              {executionRows === null ? (
                <span className="sutr-skeleton" style={{ height: 60 }} />
              ) : trace.length === 0 ? (
                <span className="sutr-meta">This tool has not been called yet.</span>
              ) : (
                <SutrTimeline entries={trace} />
              )}
            </div>
          </div>
        ) : null}
      </SutrDrawer>

      <TotpCodeDialog
        open={totpOpen}
        title="Confirm policy change"
        description="Enter your authenticator code to change how this tool executes."
        confirmLabel="Confirm"
        onClose={() => {
          setTotpOpen(false)
          setPendingMode(null)
        }}
        onSubmit={async (code) => {
          if (pendingMode) await applyMode(pendingMode, code)
        }}
      />
    </SutrPage>
  )
}
