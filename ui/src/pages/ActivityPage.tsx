import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { useSearchParams } from 'react-router-dom'
import { RefreshCw, ScrollText, ShieldCheck } from 'lucide-react'
import { LogDetailPanel } from '@/components/logs/LogDetailPanel'
import { api, ApiError, type AuditEvent, type LogEntry } from '@/api/client'
import { useCatalogStore } from '@/stores/catalog'
import { formatClock, formatDateTime, formatDuration, relativeTime } from '@/lib/format'
import {
  SutrBadge,
  SutrButton,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSearchInput,
  SutrSelect,
  SutrStatus,
  SutrTabs,
  SutrTimeline,
  describeError,
  type TimelineEntry,
} from '@/components/sutr'

type Tab = 'executions' | 'audit'

const PAGE = 60

/**
 * Activity as a timeline, because ordering is the point: what an agent asked
 * for, what the policy decided, and what came back — in the order it happened.
 */
export default function ActivityPage() {
  const [tab, setTab] = useState<Tab>('executions')

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Operate"
        title="Activity"
        subtitle="Every tool execution that passed through the gateway, and every change a person made to the configuration."
      >
        <SutrTabs
          ariaLabel="Activity kind"
          value={tab}
          onChange={setTab}
          items={[
            { value: 'executions', label: 'Tool executions' },
            { value: 'audit', label: 'Audit trail' },
          ]}
        />
      </SutrPageHeader>

      {tab === 'executions' ? <ExecutionsTab /> : <AuditTab />}
    </SutrPage>
  )
}

function ExecutionsTab() {
  const [params, setParams] = useSearchParams()
  const tools = useCatalogStore((s) => s.tools)
  const loadCatalog = useCatalogStore((s) => s.load)

  const [entries, setEntries] = useState<LogEntry[] | null>(null)
  const [outcome, setOutcome] = useState(params.get('outcome') ?? '')
  const [integration, setIntegration] = useState(params.get('integration') ?? '')
  const [tool, setTool] = useState(params.get('tool') ?? '')
  const [search, setSearch] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [exhausted, setExhausted] = useState(false)
  const [selected, setSelected] = useState<LogEntry | null>(null)

  useEffect(() => {
    void loadCatalog()
  }, [loadCatalog])

  const load = useCallback(
    async (offset: number) => {
      setLoading(true)
      try {
        const page = await api.logs.list({
          limit: PAGE,
          offset,
          outcome: outcome || undefined,
          integration: integration || undefined,
          tool: tool || undefined,
        })
        setEntries((previous) => (offset === 0 ? page : [...(previous ?? []), ...page]))
        setExhausted(page.length < PAGE)
        setError(null)
      } catch (err) {
        setError(describeError(err).message)
        if (offset === 0) setEntries([])
      } finally {
        setLoading(false)
      }
    },
    [outcome, integration, tool],
  )

  useEffect(() => {
    void load(0)
  }, [load])

  // Filters live in the URL so a filtered view can be linked to from a tool
  // page or shared with a colleague.
  useEffect(() => {
    const next = new URLSearchParams(params)
    for (const [key, value] of [
      ['outcome', outcome],
      ['integration', integration],
      ['tool', tool],
    ] as const) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    if (next.toString() !== params.toString()) setParams(next, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outcome, integration, tool])

  const integrations = useMemo(
    () => [...new Set(tools.map((t) => t.integration_id).filter(Boolean))].sort() as string[],
    [tools],
  )

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    if (!needle) return entries ?? []
    return (entries ?? []).filter(
      (entry) =>
        entry.tool_name.toLowerCase().includes(needle) ||
        entry.integration_id.toLowerCase().includes(needle) ||
        (entry.args_json ?? '').toLowerCase().includes(needle),
    )
  }, [entries, search])

  const timeline: TimelineEntry[] = visible.map((entry) => ({
    id: String(entry.id),
    time: formatClock(entry.timestamp),
    tone:
      entry.outcome === 'denied' || entry.outcome === 'error'
        ? 'danger'
        : entry.outcome === 'approval_required' || entry.outcome === 'pending'
          ? 'warning'
          : 'success',
    title: (
      <>
        <code className="sutr-mono" style={{ color: 'var(--text)' }}>
          {entry.tool_name}
        </code>
        <SutrStatus domain="outcome" value={entry.outcome} />
        {entry.access_reason ? (
          <SutrBadge tone="info" plain title="How this call was authorized">
            {entry.access_reason.replace(/_/g, ' ')}
          </SutrBadge>
        ) : null}
      </>
    ),
    detail: [
      entry.integration_id,
      formatDuration(entry.duration_ms),
      entry.api_key_label ? `key “${entry.api_key_label}”` : null,
      entry.requester_ip,
      relativeTime(entry.timestamp),
    ]
      .filter(Boolean)
      .join(' · '),
  }))

  return (
    <SutrPageBody>
      <div className="sutr-page__toolbar">
        <SutrSearchInput
          value={search}
          onValueChange={setSearch}
          ariaLabel="Search loaded executions"
          placeholder="Filter loaded executions"
          maxWidth={280}
        />
        <SutrSelect
          aria-label="Filter by outcome"
          value={outcome}
          onChange={(e) => setOutcome(e.target.value)}
          style={{ width: 180 }}
        >
          <option value="">All outcomes</option>
          <option value="executed">Executed</option>
          <option value="approval_required">Approval required</option>
          <option value="approved">Approved</option>
          <option value="denied">Denied</option>
          <option value="error">Error</option>
          <option value="pending">Pending</option>
        </SutrSelect>
        <SutrSelect
          aria-label="Filter by provider"
          value={integration}
          onChange={(e) => setIntegration(e.target.value)}
          style={{ width: 180 }}
        >
          <option value="">All providers</option>
          {integrations.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
        </SutrSelect>
        {tool ? (
          <SutrButton variant="ghost" size="sm" onClick={() => setTool('')}>
            tool: {tool} ✕
          </SutrButton>
        ) : null}
        <SutrButton
          variant="secondary"
          size="sm"
          loading={loading && entries !== null}
          onClick={() => void load(0)}
        >
          <RefreshCw size={13} /> Refresh
        </SutrButton>
      </div>

      {error ? <SutrError what="Activity could not be read." why={error} /> : null}

      {entries === null ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <span key={i} className="sutr-skeleton" style={{ height: 34 }} />
          ))}
        </div>
      ) : timeline.length === 0 ? (
        <SutrEmpty
          icon={<ScrollText size={17} />}
          title={entries.length === 0 ? 'No tool executions recorded' : 'Nothing matches'}
          body={
            entries.length === 0
              ? 'Every call an agent makes through this instance is recorded here with its policy decision, latency and outcome.'
              : 'Clear the filters to see the full timeline.'
          }
        />
      ) : (
        <>
          <SutrTimeline
            entries={timeline}
            onSelect={(entry) => {
              const match = (entries ?? []).find((row) => String(row.id) === entry.id)
              if (match) setSelected(match)
            }}
          />
          {!exhausted ? (
            <SutrButton
              variant="secondary"
              loading={loading}
              onClick={() => void load(entries.length)}
            >
              Load more
            </SutrButton>
          ) : (
            <span className="sutr-meta">
              {entries.length} execution{entries.length === 1 ? '' : 's'} loaded — that is
              everything for these filters.
            </span>
          )}
        </>
      )}

      {selected
        ? createPortal(
            <LogDetailPanel entry={selected} onClose={() => setSelected(null)} />,
            document.body,
          )
        : null}
    </SutrPageBody>
  )
}

function AuditTab() {
  const [events, setEvents] = useState<AuditEvent[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    let cancelled = false
    api.audit
      .list({ limit: 100 })
      .then((rows) => {
        if (!cancelled) setEvents(rows)
      })
      .catch((err) => {
        if (cancelled) return
        // Reading the audit trail is owner/admin only — say so plainly rather
        // than surfacing a raw 403.
        if (err instanceof ApiError && err.status === 403) setForbidden(true)
        else setError(describeError(err).message)
        setEvents([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  const timeline: TimelineEntry[] = (events ?? []).map((event) => ({
    id: String(event.id),
    time: formatClock(event.timestamp),
    tone: event.action.includes('delete') || event.action.includes('deny') ? 'danger' : 'brand',
    title: (
      <>
        <span style={{ color: 'var(--text)' }}>{event.summary}</span>
        <SutrBadge tone="neutral" plain>
          {event.action}
        </SutrBadge>
      </>
    ),
    detail: [
      event.actor_type === 'api_key'
        ? `api key ${event.actor_api_key_prefix ?? ''}`
        : event.actor_type,
      event.ip,
      formatDateTime(event.timestamp),
    ]
      .filter(Boolean)
      .join(' · '),
  }))

  return (
    <SutrPageBody>
      {forbidden ? (
        <SutrEmpty
          icon={<ShieldCheck size={17} />}
          title="The audit trail is restricted"
          body="Only organisation owners and admins can read configuration history. Ask an owner if you need access."
        />
      ) : error ? (
        <SutrError what="The audit trail could not be read." why={error} />
      ) : events === null ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {[0, 1, 2, 3].map((i) => (
            <span key={i} className="sutr-skeleton" style={{ height: 34 }} />
          ))}
        </div>
      ) : timeline.length === 0 ? (
        <SutrEmpty
          icon={<ShieldCheck size={17} />}
          title="No configuration changes recorded"
          body="Policy changes, approvals, key creation and member changes are written here as they happen."
        />
      ) : (
        <SutrTimeline entries={timeline} />
      )}
    </SutrPageBody>
  )
}
