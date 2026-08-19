import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, Clock, Inbox, Loader2, ShieldQuestion, XCircle } from 'lucide-react'
import { api, type ApprovalRequest } from '@/api/client'

const FILTERS = [
  { key: 'pending', label: 'Pending' },
  { key: 'approved', label: 'Approved' },
  { key: 'denied', label: 'Denied' },
  { key: '', label: 'All' },
] as const

const STATUS_TONE: Record<string, { bg: string; text: string }> = {
  pending: { bg: 'var(--badge-amber-bg)', text: 'var(--badge-amber-text)' },
  approved: { bg: 'var(--badge-green-bg)', text: 'var(--badge-green-text)' },
  consumed: { bg: 'var(--badge-green-bg)', text: 'var(--badge-green-text)' },
  auto_approved: { bg: 'var(--badge-blue-bg)', text: 'var(--badge-blue-text)' },
  denied: { bg: 'var(--badge-red-bg)', text: 'var(--badge-red-text)' },
  expired: { bg: 'var(--badge-gray-bg)', text: 'var(--badge-gray-text)' },
}

function parseUtc(value: string): Date {
  // Server timestamps are naive UTC; without the Z the browser reads them local.
  return new Date(/[Z+]/.test(value) ? value : `${value}Z`)
}

function relative(value: string): string {
  const diff = Date.now() - parseUtc(value).getTime()
  const secs = Math.round(Math.abs(diff) / 1000)
  const past = diff >= 0
  const unit =
    secs < 60
      ? `${secs}s`
      : secs < 3600
        ? `${Math.floor(secs / 60)}m`
        : secs < 86400
          ? `${Math.floor(secs / 3600)}h`
          : `${Math.floor(secs / 86400)}d`
  return past ? `${unit} ago` : `in ${unit}`
}

function effectiveStatus(request: ApprovalRequest): string {
  if (request.status === 'pending' && parseUtc(request.expires_at) <= new Date()) {
    return 'expired'
  }
  return request.status
}

export default function ApprovalsPage() {
  const [filter, setFilter] = useState<string>('pending')
  const [requests, setRequests] = useState<ApprovalRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async (status: string) => {
    setLoading(true)
    try {
      setRequests(await api.approvals.list({ status: status || undefined, limit: 100 }))
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load approvals')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(filter)
  }, [filter, load])

  // Pending requests expire on a clock, so keep the queue fresh while it's open.
  useEffect(() => {
    if (filter !== 'pending') return
    const timer = window.setInterval(() => load(filter), 15_000)
    return () => window.clearInterval(timer)
  }, [filter, load])

  const grouped = useMemo(() => {
    const live: ApprovalRequest[] = []
    const stale: ApprovalRequest[] = []
    for (const request of requests) {
      ;(effectiveStatus(request) === 'expired' ? stale : live).push(request)
    }
    return { live, stale }
  }, [requests])

  return (
    <div style={{ flex: 1, overflow: 'auto', background: 'var(--bg)', padding: '28px 24px 80px' }}>
      <div style={{ maxWidth: 820, margin: '0 auto' }}>
        <div style={{ marginBottom: 16 }}>
          <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600, color: 'var(--text)' }}>
            Approvals
          </h1>
          <p style={{ margin: '3px 0 0', fontSize: 12.5, color: 'var(--text-dim)' }}>
            Tool calls waiting on a human decision. Approving binds to the exact arguments shown.
          </p>
        </div>

        <div style={{ display: 'flex', gap: 4, marginBottom: 14 }}>
          {FILTERS.map((option) => {
            const active = filter === option.key
            return (
              <button
                key={option.key || 'all'}
                type="button"
                onClick={() => setFilter(option.key)}
                aria-pressed={active}
                style={{
                  padding: '5px 11px',
                  fontSize: 12,
                  borderRadius: 6,
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                  border: `1px solid ${active ? 'var(--text)' : 'var(--border)'}`,
                  background: active ? 'var(--content-bg)' : 'var(--surface)',
                  color: active ? 'var(--text)' : 'var(--text-dim)',
                  fontWeight: active ? 600 : 400,
                }}
              >
                {option.label}
              </button>
            )
          })}
        </div>

        {error && (
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
            {error}
          </div>
        )}

        {loading && requests.length === 0 ? (
          <div style={{ padding: 48, textAlign: 'center', color: 'var(--text-faint)' }}>
            <Loader2 size={18} style={{ animation: 'spin 1s linear infinite' }} />
          </div>
        ) : requests.length === 0 ? (
          <EmptyState filter={filter} />
        ) : (
          <>
            <RequestList requests={grouped.live} />
            {grouped.stale.length > 0 && (
              <>
                <div
                  style={{
                    fontSize: 10,
                    fontWeight: 600,
                    textTransform: 'uppercase',
                    letterSpacing: 0.6,
                    color: 'var(--text-faint)',
                    margin: '18px 0 8px',
                  }}
                >
                  Expired · {grouped.stale.length}
                </div>
                <RequestList requests={grouped.stale} muted />
              </>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function RequestList({ requests, muted }: { requests: ApprovalRequest[]; muted?: boolean }) {
  if (requests.length === 0) return null
  return (
    <div
      style={{
        background: 'var(--content-bg)',
        border: '1px solid var(--border)',
        borderRadius: 10,
        overflow: 'hidden',
        opacity: muted ? 0.65 : 1,
      }}
    >
      {requests.map((request, index) => (
        <RequestRow key={request.id} request={request} first={index === 0} />
      ))}
    </div>
  )
}

function RequestRow({ request, first }: { request: ApprovalRequest; first: boolean }) {
  const status = effectiveStatus(request)
  const tone = STATUS_TONE[status] ?? STATUS_TONE.expired!
  const actionable = status === 'pending'

  let args: Record<string, unknown> = {}
  try {
    args = JSON.parse(request.args_json || '{}')
  } catch {
    /* keep empty on malformed args */
  }
  const argKeys = Object.keys(args)

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 12,
        padding: '12px 16px',
        borderTop: first ? 'none' : '1px solid var(--border)',
      }}
    >
      <span style={{ marginTop: 2, flexShrink: 0 }}>
        {status === 'pending' ? (
          <ShieldQuestion size={16} style={{ color: 'var(--badge-amber-dot)' }} />
        ) : status === 'denied' ? (
          <XCircle size={16} style={{ color: 'var(--badge-red-dot)' }} />
        ) : status === 'expired' ? (
          <Clock size={16} style={{ color: 'var(--text-faint)' }} />
        ) : (
          <CheckCircle2 size={16} style={{ color: 'var(--badge-green-dot)' }} />
        )}
      </span>

      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: 'var(--text)',
              fontFamily: 'var(--font-mono)',
            }}
          >
            {request.tool_name}
          </span>
          <span style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>{request.integration_id}</span>
          <span
            style={{
              fontSize: 10,
              fontWeight: 600,
              padding: '2px 7px',
              borderRadius: 999,
              background: tone.bg,
              color: tone.text,
              textTransform: 'uppercase',
              letterSpacing: 0.4,
            }}
          >
            {status.replace('_', ' ')}
          </span>
        </div>

        <div style={{ fontSize: 12.5, color: 'var(--text-dim)', marginTop: 3 }}>
          {request.summary_text}
        </div>

        {request.additional_info && (
          <div
            style={{
              fontSize: 12,
              color: 'var(--text-dim)',
              marginTop: 5,
              paddingLeft: 8,
              borderLeft: '2px solid var(--border-strong)',
            }}
          >
            “{request.additional_info}”
          </div>
        )}

        <div
          style={{
            display: 'flex',
            gap: 10,
            flexWrap: 'wrap',
            fontSize: 11,
            color: 'var(--text-faint)',
            marginTop: 6,
          }}
        >
          <span>requested {relative(request.requested_at)}</span>
          {status === 'pending' && <span>expires {relative(request.expires_at)}</span>}
          {argKeys.length > 0 && (
            <span style={{ fontFamily: 'var(--font-mono)' }}>
              {argKeys.slice(0, 4).join(', ')}
              {argKeys.length > 4 ? ` +${argKeys.length - 4}` : ''}
            </span>
          )}
          {request.api_key_label && <span>via key “{request.api_key_label}”</span>}
          {request.requester_ip && <span>{request.requester_ip}</span>}
        </div>
      </div>

      {actionable && (
        <Link
          to={`/approve/${request.id}`}
          style={{
            flexShrink: 0,
            display: 'inline-flex',
            alignItems: 'center',
            height: 30,
            padding: '0 12px',
            borderRadius: 7,
            background: 'var(--text)',
            color: 'var(--content-bg)',
            fontSize: 12.5,
            fontWeight: 600,
            textDecoration: 'none',
          }}
        >
          Review
        </Link>
      )}
    </div>
  )
}

function EmptyState({ filter }: { filter: string }) {
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
      <Inbox size={22} style={{ color: 'var(--text-faint)', marginBottom: 10 }} />
      <div style={{ fontSize: 13.5, fontWeight: 600, color: 'var(--text)', marginBottom: 4 }}>
        {filter === 'pending' ? 'Nothing waiting on you' : 'No approval requests'}
      </div>
      <div style={{ fontSize: 12.5 }}>
        {filter === 'pending'
          ? 'Requests appear here when an agent calls a tool that needs approval.'
          : 'Try a different filter.'}
      </div>
    </div>
  )
}
