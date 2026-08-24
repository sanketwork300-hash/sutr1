import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Inbox, ShieldQuestion } from 'lucide-react'
import { api, type ApprovalRequest } from '@/api/client'
import { useCatalogStore } from '@/stores/catalog'
import { isTotpChallengeError } from '@/lib/totpError'
import { TotpCodeDialog } from '@/components/totp/TotpCodeDialog'
import { formatDateTime, parseTimestamp, prettyJson, relativeTime } from '@/lib/format'
import {
  SutrButton,
  SutrCodeBlock,
  SutrDefinitionList,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSectionLabel,
  SutrStatus,
  SutrTable,
  SutrTabs,
  describeError,
  type Column,
} from '@/components/sutr'

type Filter = 'pending' | 'approved' | 'denied' | 'all'
type Decision = 'approve-once' | 'approve-exact' | 'allow-tool' | 'deny'

const DECISION_LABEL: Record<Decision, string> = {
  'approve-once': 'Approve this request',
  'approve-exact': 'Always allow these exact arguments',
  'allow-tool': 'Allow this tool for any arguments',
  deny: 'Deny',
}

/** A pending request whose expiry has passed is expired in practice, even
 *  though the row is only rewritten by the maintenance loop. */
function effectiveStatus(request: ApprovalRequest): string {
  if (request.status === 'pending' && parseTimestamp(request.expires_at) <= new Date()) {
    return 'expired'
  }
  return request.status
}

/**
 * A security operations queue. Each request shows the exact arguments a person
 * is being asked to authorize, who asked, and how long the window stays open —
 * and every decision states precisely what it grants.
 */
export default function ApprovalsPage() {
  const [filter, setFilter] = useState<Filter>('pending')
  const [requests, setRequests] = useState<ApprovalRequest[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<ApprovalRequest | null>(null)
  const [busy, setBusy] = useState<Decision | null>(null)
  const [decisionError, setDecisionError] = useState<string | null>(null)
  const [totpOpen, setTotpOpen] = useState(false)
  const [pending, setPending] = useState<Decision | null>(null)
  const loadPendingApprovals = useCatalogStore((s) => s.loadPendingApprovals)

  const load = useCallback(async (status: Filter) => {
    try {
      const list = await api.approvals.list({
        status: status === 'all' ? undefined : status,
        limit: 100,
      })
      setRequests(list)
      setError(null)
    } catch (err) {
      setError(describeError(err).message)
      setRequests([])
    }
  }, [])

  useEffect(() => {
    void load(filter)
  }, [filter, load])

  // Pending requests expire on a clock, so keep the queue honest while it is open.
  useEffect(() => {
    if (filter !== 'pending') return
    const timer = window.setInterval(() => void load(filter), 15_000)
    return () => window.clearInterval(timer)
  }, [filter, load])

  const { live, expired } = useMemo(() => {
    const liveRows: ApprovalRequest[] = []
    const expiredRows: ApprovalRequest[] = []
    for (const request of requests ?? []) {
      ;(effectiveStatus(request) === 'expired' ? expiredRows : liveRows).push(request)
    }
    return { live: liveRows, expired: expiredRows }
  }, [requests])

  async function decide(action: Decision, totpCode?: string) {
    if (!selected) return
    setBusy(action)
    setDecisionError(null)
    try {
      const call =
        action === 'approve-once'
          ? api.approvals.approveOnce
          : action === 'approve-exact'
            ? api.approvals.approveExact
            : action === 'allow-tool'
              ? api.approvals.allowTool
              : api.approvals.deny
      const updated = await call(selected.id, totpCode)
      setSelected(updated)
      setPending(null)
      await load(filter)
      await loadPendingApprovals()
    } catch (err) {
      if (isTotpChallengeError(err)) {
        setPending(action)
        setTotpOpen(true)
        return
      }
      setDecisionError(describeError(err).message)
    } finally {
      setBusy(null)
    }
  }

  const columns: Column<ApprovalRequest>[] = [
    {
      key: 'tool',
      header: 'Tool',
      render: (request) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          <code className="sutr-mono sutr-table__primary">{request.tool_name}</code>
          <span className="sutr-meta sutr-truncate" style={{ maxWidth: 380 }}>
            {request.summary_text}
          </span>
        </div>
      ),
    },
    {
      key: 'integration',
      header: 'Provider',
      width: 130,
      render: (request) => <span className="sutr-mono">{request.integration_id}</span>,
    },
    {
      key: 'requester',
      header: 'Requested by',
      width: 180,
      render: (request) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <span>{request.api_key_label ?? request.requested_by_agent ?? 'unknown caller'}</span>
          <span className="sutr-meta sutr-mono">{request.requester_ip ?? '—'}</span>
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      width: 170,
      render: (request) => <SutrStatus domain="approval" value={effectiveStatus(request)} />,
    },
    {
      key: 'requested',
      header: 'Requested',
      width: 130,
      render: (request) => (
        <span className="sutr-meta" title={formatDateTime(request.requested_at)}>
          {relativeTime(request.requested_at)}
        </span>
      ),
    },
    {
      key: 'expires',
      header: 'Expires',
      width: 130,
      render: (request) =>
        effectiveStatus(request) === 'pending' ? (
          <span
            className="sutr-meta"
            style={{ color: 'var(--amber)' }}
            title={formatDateTime(request.expires_at)}
          >
            {relativeTime(request.expires_at)}
          </span>
        ) : (
          <span className="sutr-meta">—</span>
        ),
    },
  ]

  const status = selected ? effectiveStatus(selected) : null
  const actionable = status === 'pending'

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Operate"
        title="Approvals"
        subtitle="Tool calls held for a human decision. An approval authorizes the exact request shown — not the tool, and not the next call."
      >
        <SutrTabs
          ariaLabel="Approval status"
          value={filter}
          onChange={setFilter}
          items={[
            {
              value: 'pending',
              label: 'Pending',
              count: filter === 'pending' ? live.length : undefined,
            },
            { value: 'approved', label: 'Approved' },
            { value: 'denied', label: 'Denied' },
            { value: 'all', label: 'All' },
          ]}
        />
      </SutrPageHeader>

      <SutrPageBody>
        {error ? <SutrError what="The approval queue could not be read." why={error} /> : null}

        {requests === null ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {[0, 1, 2].map((i) => (
              <span key={i} className="sutr-skeleton" style={{ height: 46 }} />
            ))}
          </div>
        ) : (
          <>
            <SutrTable
              columns={columns}
              rows={live}
              minWidth={900}
              rowKey={(request) => request.id}
              onRowClick={(request) => {
                setSelected(request)
                setDecisionError(null)
              }}
              caption="Approval requests"
              empty={
                <SutrEmpty
                  icon={filter === 'pending' ? <ShieldQuestion size={17} /> : <Inbox size={17} />}
                  title={filter === 'pending' ? 'Nothing waiting on you' : 'No approval requests'}
                  body={
                    filter === 'pending'
                      ? 'A request appears here the moment an agent calls a tool whose policy is set to ask. Until someone decides, the call does not execute.'
                      : 'No requests match this filter.'
                  }
                />
              }
            />

            {expired.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <SutrSectionLabel>Expired · {expired.length}</SutrSectionLabel>
                <div style={{ opacity: 0.7 }}>
                  <SutrTable
                    columns={columns}
                    rows={expired}
                    minWidth={900}
                    rowKey={(request) => request.id}
                    onRowClick={(request) => setSelected(request)}
                  />
                </div>
              </div>
            ) : null}
          </>
        )}
      </SutrPageBody>

      <SutrDrawer
        open={Boolean(selected)}
        onClose={() => {
          setSelected(null)
          setDecisionError(null)
        }}
        wide
        title={selected ? <code className="sutr-mono">{selected.tool_name}</code> : ''}
        subtitle={selected ? `${selected.integration_id} · ${selected.summary_text}` : ''}
        actions={
          selected ? <SutrStatus domain="approval" value={effectiveStatus(selected)} /> : null
        }
        footer={
          selected && actionable ? (
            <>
              <SutrButton
                variant="danger"
                loading={busy === 'deny'}
                onClick={() => void decide('deny')}
              >
                Deny
              </SutrButton>
              <span style={{ flex: 1 }} />
              <SutrButton
                variant="brand"
                loading={busy === 'approve-once'}
                onClick={() => void decide('approve-once')}
              >
                Approve this request
              </SutrButton>
            </>
          ) : (
            <Link to={`/approve/${selected?.id ?? ''}`} className="sutr-btn sutr-btn--secondary">
              Open the full record
            </Link>
          )
        }
      >
        {selected ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
            {actionable ? (
              <div
                style={{
                  display: 'flex',
                  gap: 10,
                  padding: 12,
                  borderRadius: 'var(--r-sm)',
                  border: '1px solid var(--brand-border)',
                  background: 'var(--brand-soft)',
                }}
              >
                <ShieldQuestion
                  size={15}
                  style={{ color: 'var(--brand)', flexShrink: 0, marginTop: 1 }}
                />
                <span className="sutr-body" style={{ color: 'var(--text)' }}>
                  Approval applies only to the exact request below. Any change to these arguments
                  goes back through the gate.
                </span>
              </div>
            ) : null}

            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              <SutrSectionLabel>Exact arguments</SutrSectionLabel>
              <SutrCodeBlock
                label="arguments"
                maxHeight={280}
                code={prettyJson(selected.args_json) || '{}'}
              />
              <span className="sutr-meta sutr-mono">hash {selected.args_hash}</span>
            </div>

            {selected.additional_info ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <SutrSectionLabel>Why the agent says it needs this</SutrSectionLabel>
                <p
                  className="sutr-body"
                  style={{
                    paddingLeft: 10,
                    borderLeft: '2px solid var(--border-strong)',
                    color: 'var(--text)',
                  }}
                >
                  “{selected.additional_info}”
                </p>
                <span className="sutr-meta">
                  Supplied by the caller. Treat it as a claim, not as evidence.
                </span>
              </div>
            ) : null}

            <SutrDefinitionList
              items={[
                {
                  key: 'Requester',
                  value: selected.api_key_label ?? selected.requested_by_agent ?? 'unknown caller',
                },
                {
                  key: 'API key',
                  value: selected.api_key_prefix ? (
                    <code className="sutr-mono">{selected.api_key_prefix}…</code>
                  ) : (
                    <span className="sutr-meta">not an API key</span>
                  ),
                },
                {
                  key: 'IP',
                  value: <code className="sutr-mono">{selected.requester_ip ?? '—'}</code>,
                },
                {
                  key: 'User agent',
                  value: (
                    <span className="sutr-meta" style={{ overflowWrap: 'anywhere' }}>
                      {selected.user_agent ?? '—'}
                    </span>
                  ),
                },
                { key: 'Requested', value: formatDateTime(selected.requested_at) },
                {
                  key: 'Expires',
                  value: `${formatDateTime(selected.expires_at)} (${relativeTime(selected.expires_at)})`,
                },
                {
                  key: 'Decision',
                  value: selected.decided_at ? (
                    `${selected.decision_mode ?? 'decided'} · ${formatDateTime(selected.decided_at)}`
                  ) : (
                    <span className="sutr-meta">not decided</span>
                  ),
                },
              ]}
            />

            {actionable ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <SutrSectionLabel>Broader grants</SutrSectionLabel>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <SutrButton
                    variant="secondary"
                    size="sm"
                    loading={busy === 'approve-exact'}
                    onClick={() => void decide('approve-exact')}
                  >
                    {DECISION_LABEL['approve-exact']}
                  </SutrButton>
                  <SutrButton
                    variant="secondary"
                    size="sm"
                    loading={busy === 'allow-tool'}
                    onClick={() => {
                      if (
                        window.confirm(
                          `Allow every future call to ${selected.tool_name}, whatever the arguments? This changes the tool's policy to auto approve.`,
                        )
                      ) {
                        void decide('allow-tool')
                      }
                    }}
                  >
                    {DECISION_LABEL['allow-tool']}
                  </SutrButton>
                </div>
                <span className="sutr-meta">
                  “Always allow these exact arguments” keeps the gate for anything different. “Allow
                  this tool” removes the gate entirely.
                </span>
              </div>
            ) : null}

            {decisionError ? (
              <SutrError
                what="The decision was not recorded."
                why={decisionError}
                meta={{ request: selected.id }}
              />
            ) : null}
          </div>
        ) : null}
      </SutrDrawer>

      <TotpCodeDialog
        open={totpOpen}
        title="Confirm decision"
        description={
          pending
            ? `Enter your authenticator code to ${DECISION_LABEL[pending].toLowerCase()}.`
            : 'Enter your authenticator code to continue.'
        }
        confirmLabel="Confirm"
        onClose={() => {
          setTotpOpen(false)
          setPending(null)
        }}
        onSubmit={async (code) => {
          if (pending) await decide(pending, code)
        }}
      />
    </SutrPage>
  )
}
