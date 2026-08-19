import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { FileClock, Loader2, ScrollText, ShieldCheck } from 'lucide-react'
import { LogCard } from '@/components/logs/LogCard'
import { LogDetailPanel } from '@/components/logs/LogDetailPanel'
import { api, ApiError, type AuditEvent, type LogEntry } from '@/api/client'

type Tab = 'logs' | 'audit'

const OUTCOMES = ['', 'executed', 'pending', 'denied', 'error'] as const

export default function ActivityPage() {
  const [tab, setTab] = useState<Tab>('logs')

  return (
    <div style={{ flex: 1, overflow: 'auto', background: 'var(--bg)', padding: '28px 24px 80px' }}>
      <div style={{ maxWidth: 900, margin: '0 auto' }}>
        <div style={{ marginBottom: 16 }}>
          <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600, color: 'var(--text)' }}>
            Activity
          </h1>
          <p style={{ margin: '3px 0 0', fontSize: 12.5, color: 'var(--text-dim)' }}>
            What your agents did, and what people changed.
          </p>
        </div>

        <div style={{ display: 'flex', gap: 4, marginBottom: 14 }}>
          <TabButton active={tab === 'logs'} onClick={() => setTab('logs')} icon={<ScrollText size={13} />}>
            Tool calls
          </TabButton>
          <TabButton active={tab === 'audit'} onClick={() => setTab('audit')} icon={<ShieldCheck size={13} />}>
            Audit trail
          </TabButton>
        </div>

        {tab === 'logs' ? <LogsTab /> : <AuditTab />}
      </div>
    </div>
  )
}

function LogsTab() {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [outcome, setOutcome] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<LogEntry | null>(null)

  const load = useCallback(async (filterOutcome: string) => {
    setLoading(true)
    try {
      setEntries(await api.logs.list({ limit: 100, outcome: filterOutcome || undefined }))
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load logs')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(outcome)
  }, [outcome, load])

  return (
    <>
      <div style={{ display: 'flex', gap: 4, marginBottom: 12, flexWrap: 'wrap' }}>
        {OUTCOMES.map((value) => (
          <FilterChip
            key={value || 'all'}
            active={outcome === value}
            onClick={() => setOutcome(value)}
          >
            {value || 'All'}
          </FilterChip>
        ))}
      </div>

      {error && <Banner>{error}</Banner>}

      {loading && entries.length === 0 ? (
        <Spinner />
      ) : entries.length === 0 ? (
        <Empty
          icon={<ScrollText size={22} />}
          title="No tool calls yet"
          hint="Every call an agent makes through Sutr shows up here, with its policy decision."
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {entries.map((entry) => (
            <LogCard
              key={entry.id}
              entry={entry}
              onClick={() => setSelected(entry)}
              onAction={() => load(outcome)}
            />
          ))}
        </div>
      )}

      {selected &&
        createPortal(
          <LogDetailPanel entry={selected} onClose={() => setSelected(null)} />,
          document.body,
        )}
    </>
  )
}

function AuditTab() {
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [forbidden, setForbidden] = useState(false)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const rows = await api.audit.list({ limit: 100 })
        if (!cancelled) setEvents(rows)
      } catch (e) {
        if (cancelled) return
        // Reading the audit trail is owner/admin only — say so plainly rather
        // than showing a raw 403.
        if (e instanceof ApiError && e.status === 403) setForbidden(true)
        else setError(e instanceof Error ? e.message : 'Failed to load the audit trail')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (loading) return <Spinner />
  if (forbidden) {
    return (
      <Empty
        icon={<ShieldCheck size={22} />}
        title="Owners and admins only"
        hint="The audit trail records who changed policies, keys, members, and integrations. Ask an owner for access."
      />
    )
  }
  if (error) return <Banner>{error}</Banner>
  if (events.length === 0) {
    return (
      <Empty
        icon={<FileClock size={22} />}
        title="No audit events yet"
        hint="Logins, policy changes, key management, approvals, and integration changes are recorded here."
      />
    )
  }

  return (
    <div
      style={{
        background: 'var(--content-bg)',
        border: '1px solid var(--border)',
        borderRadius: 10,
        overflow: 'hidden',
      }}
    >
      {events.map((event, index) => (
        <AuditRow key={event.id} event={event} first={index === 0} />
      ))}
    </div>
  )
}

function AuditRow({ event, first }: { event: AuditEvent; first: boolean }) {
  const [open, setOpen] = useState(false)
  let metadata: Record<string, unknown> = {}
  try {
    metadata = JSON.parse(event.metadata_json || '{}')
  } catch {
    /* ignore malformed metadata */
  }
  const hasMetadata = Object.keys(metadata).length > 0

  return (
    <div style={{ borderTop: first ? 'none' : '1px solid var(--border)' }}>
      <button
        type="button"
        onClick={() => hasMetadata && setOpen((value) => !value)}
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: 10,
          width: '100%',
          textAlign: 'left',
          padding: '10px 16px',
          border: 'none',
          background: 'transparent',
          cursor: hasMetadata ? 'pointer' : 'default',
          fontFamily: 'inherit',
        }}
      >
        <span
          style={{
            fontSize: 11.5,
            fontFamily: 'var(--font-mono)',
            color: 'var(--text-dim)',
            flexShrink: 0,
            minWidth: 128,
          }}
        >
          {new Date(/[Z+]/.test(event.timestamp) ? event.timestamp : `${event.timestamp}Z`)
            .toLocaleString()
            .replace(',', '')}
        </span>
        <span
          style={{
            fontSize: 11,
            fontFamily: 'var(--font-mono)',
            color: 'var(--syn-tag)',
            flexShrink: 0,
            minWidth: 190,
          }}
        >
          {event.action}
        </span>
        <span style={{ fontSize: 12.5, color: 'var(--text)', flex: 1, minWidth: 0 }}>
          {event.summary}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-faint)', flexShrink: 0 }}>
          {event.actor_type === 'api_key'
            ? `key ${event.actor_api_key_prefix ?? ''}…`
            : event.actor_type === 'system'
              ? 'system'
              : (event.ip ?? 'user')}
        </span>
      </button>

      {open && hasMetadata && (
        <pre
          style={{
            margin: 0,
            padding: '8px 16px 12px 154px',
            fontSize: 11.5,
            fontFamily: 'var(--font-mono)',
            color: 'var(--text-dim)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {JSON.stringify(metadata, null, 2)}
        </pre>
      )}
    </div>
  )
}

function TabButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '6px 12px',
        fontSize: 12.5,
        borderRadius: 7,
        cursor: 'pointer',
        fontFamily: 'inherit',
        border: `1px solid ${active ? 'var(--text)' : 'var(--border)'}`,
        background: active ? 'var(--content-bg)' : 'var(--surface)',
        color: active ? 'var(--text)' : 'var(--text-dim)',
        fontWeight: active ? 600 : 400,
      }}
    >
      {icon}
      {children}
    </button>
  )
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      style={{
        padding: '4px 10px',
        fontSize: 11.5,
        borderRadius: 999,
        cursor: 'pointer',
        fontFamily: 'inherit',
        textTransform: 'capitalize',
        border: `1px solid ${active ? 'var(--text)' : 'var(--border)'}`,
        background: active ? 'var(--content-bg)' : 'var(--surface)',
        color: active ? 'var(--text)' : 'var(--text-dim)',
      }}
    >
      {children}
    </button>
  )
}

function Spinner() {
  return (
    <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-faint)' }}>
      <Loader2 size={18} style={{ animation: 'spin 1s linear infinite' }} />
    </div>
  )
}

function Banner({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        marginBottom: 14,
        padding: '8px 12px',
        borderRadius: 8,
        fontSize: 12.5,
        background: 'var(--badge-red-bg)',
        color: 'var(--badge-red-text)',
      }}
    >
      {children}
    </div>
  )
}

function Empty({
  icon,
  title,
  hint,
}: {
  icon: React.ReactNode
  title: string
  hint: string
}) {
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
      <span style={{ color: 'var(--text-faint)', display: 'inline-block', marginBottom: 10 }}>
        {icon}
      </span>
      <div style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>
        {title}
      </div>
      <div style={{ fontSize: 12.5, maxWidth: 460, margin: '0 auto' }}>{hint}</div>
    </div>
  )
}
