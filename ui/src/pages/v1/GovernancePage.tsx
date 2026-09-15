import { useState } from 'react'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSpinner,
  SutrTable,
  SutrTabs,
  SutrTextarea,
  describeError,
  type Column,
} from '@/components/sutr'
import {
  formatWhen,
  v1,
  type ComplianceRun,
  type GovernanceException,
  type GovernancePolicy,
  type GovernanceReview,
} from '@/api/v1'
import { useAction, useResource } from './useResource'

type Tab = 'policies' | 'evaluate' | 'compliance' | 'reviews' | 'exceptions'

const STATE_TONE: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'neutral'> = {
  active: 'success',
  approved: 'success',
  passed: 'success',
  draft: 'neutral',
  review: 'warning',
  pending: 'warning',
  blocked: 'danger',
  failed: 'danger',
  rejected: 'danger',
  revoked: 'danger',
  expired: 'neutral',
}

const SAMPLE_REQUEST = `{
  "action": "tool.invoke",
  "resource_type": "tool",
  "resource_id": "00000000-0000-0000-0000-000000000000",
  "context": {}
}`

/**
 * Policy, compliance, risk, review and exceptions (LLD §5.2).
 *
 * The decision point is separated from the enforcement point, which is what
 * makes the Evaluate tab meaningful: it asks the same question the gateway
 * asks on a live call and changes nothing, so an operator can find out *why*
 * something is refused without causing it to be refused again.
 */
export default function GovernancePage() {
  const [tab, setTab] = useState<Tab>('policies')
  const action = useAction()

  const policies = useResource(() => v1.governance.policies(), [])
  const runs = useResource(() => v1.governance.complianceRuns(), [])
  const reviews = useResource(() => v1.governance.reviews(), [])
  const exceptions = useResource(() => v1.governance.exceptions(), [])

  const [request, setRequest] = useState(SAMPLE_REQUEST)
  const [decision, setDecision] = useState<Record<string, unknown> | null>(null)
  const [evaluating, setEvaluating] = useState(false)
  const [evaluateError, setEvaluateError] = useState<string | null>(null)

  async function evaluate() {
    setEvaluating(true)
    setEvaluateError(null)
    setDecision(null)
    try {
      setDecision(await v1.governance.evaluate(JSON.parse(request)))
    } catch (caught) {
      setEvaluateError(
        caught instanceof SyntaxError
          ? 'That is not valid JSON, so nothing was sent.'
          : describeError(caught).message,
      )
    } finally {
      setEvaluating(false)
    }
  }

  async function decideReview(review: GovernanceReview, verdict: string) {
    const ok = await action.run(
      () => v1.governance.decideReview(review.id, verdict),
      `Review ${verdict}.`,
    )
    if (ok) reviews.reload()
  }

  async function decideException(exception: GovernanceException, verdict: string) {
    const ok = await action.run(
      () => v1.governance.decideException(exception.id, verdict),
      `Exception ${verdict}.`,
    )
    if (ok) exceptions.reload()
  }

  const policyColumns: Column<GovernancePolicy>[] = [
    {
      key: 'name',
      header: 'Policy',
      render: (row) => (
        <>
          <strong>{row.name}</strong>
          <div className="sutr-muted">{row.description ?? row.key ?? ''}</div>
        </>
      ),
    },
    {
      key: 'active',
      header: 'Active version',
      width: 130,
      render: (row) =>
        row.active_version ? (
          `v${row.active_version}`
        ) : (
          // No active version means nothing is being enforced from this
          // policy — worth saying rather than showing a dash.
          <span className="sutr-muted">none enforced</span>
        ),
    },
    {
      key: 'versions',
      header: 'Versions',
      render: (row) => (
        <span style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {(row.versions ?? []).map((version) => (
            <SutrBadge key={version.version} tone={STATE_TONE[version.state] ?? 'neutral'}>
              v{version.version} {version.state}
            </SutrBadge>
          ))}
        </span>
      ),
    },
  ]

  const runColumns: Column<ComplianceRun>[] = [
    {
      key: 'framework',
      header: 'Framework',
      render: (row) => <strong>{row.framework ?? '—'}</strong>,
    },
    {
      key: 'status',
      header: 'Status',
      width: 120,
      render: (row) => (
        <SutrBadge tone={STATE_TONE[row.status ?? ''] ?? 'neutral'} dot>
          {row.status ?? 'unknown'}
        </SutrBadge>
      ),
    },
    {
      key: 'score',
      header: 'Score',
      numeric: true,
      width: 90,
      render: (row) => (row.score === null || row.score === undefined ? '—' : row.score),
    },
    { key: 'created', header: 'Run', width: 180, render: (row) => formatWhen(row.created_at) },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Govern"
        title="Governance"
        subtitle="Versioned policy, compliance runs, risk, the approval workflow, and the exceptions that all expire."
      >
        <SutrTabs
          items={[
            { value: 'policies', label: 'Policies', count: policies.data?.length },
            { value: 'evaluate', label: 'Evaluate' },
            { value: 'compliance', label: 'Compliance', count: runs.data?.length },
            { value: 'reviews', label: 'Reviews', count: reviews.data?.length },
            { value: 'exceptions', label: 'Exceptions', count: exceptions.data?.length },
          ]}
          value={tab}
          onChange={setTab}
          ariaLabel="Governance view"
        />
      </SutrPageHeader>

      <SutrPageBody>
        {action.error ? <SutrError what={action.error} /> : null}
        {action.message ? (
          <SutrCard>
            <SutrCardBody>{action.message}</SutrCardBody>
          </SutrCard>
        ) : null}

        {tab === 'policies' ? (
          <>
            {policies.error ? <SutrError what={policies.error} /> : null}
            {policies.loading ? <SutrSpinner /> : null}
            {policies.data ? (
              policies.data.length ? (
                <SutrTable
                  columns={policyColumns}
                  rows={policies.data}
                  rowKey={(row) => row.id}
                  minWidth={720}
                />
              ) : (
                <SutrEmpty
                  title="No policies"
                  body="A tenant with no policies loses nothing: the gate refuses on failure and nothing here silently permits."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'evaluate' ? (
          <>
            <SutrCard>
              <SutrCardHeader
                title="Ask the decision point"
                meta="Read-only — evaluating changes nothing"
              />
              <SutrCardBody>
                <SutrTextarea
                  value={request}
                  onChange={(event) => setRequest(event.target.value)}
                  rows={10}
                  aria-label="Authorization request"
                  style={{ fontFamily: 'monospace', width: '100%' }}
                />
                <div style={{ marginTop: 8 }}>
                  <SutrButton onClick={evaluate} disabled={evaluating}>
                    Evaluate
                  </SutrButton>
                </div>
              </SutrCardBody>
            </SutrCard>

            {evaluateError ? <SutrError what={evaluateError} /> : null}
            {evaluating ? <SutrSpinner /> : null}
            {decision ? (
              <SutrCard>
                <SutrCardHeader title="Decision" />
                <SutrCardBody>
                  <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                    {JSON.stringify(decision, null, 2)}
                  </pre>
                </SutrCardBody>
              </SutrCard>
            ) : null}
          </>
        ) : null}

        {tab === 'compliance' ? (
          <>
            {runs.error ? <SutrError what={runs.error} /> : null}
            {runs.data ? (
              runs.data.length ? (
                <>
                  <SutrCard>
                    <SutrCardBody className="sutr-muted">
                      A compliance run checks controls. It does not certify anything, and a control
                      that could not be checked is reported as unchecked rather than as passing.
                    </SutrCardBody>
                  </SutrCard>
                  <SutrTable
                    columns={runColumns}
                    rows={runs.data}
                    rowKey={(row) => row.id}
                    minWidth={620}
                  />
                </>
              ) : (
                <SutrEmpty title="No compliance runs yet" />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'reviews' ? (
          <>
            {reviews.error ? <SutrError what={reviews.error} /> : null}
            {reviews.data ? (
              reviews.data.length ? (
                <SutrTable
                  columns={[
                    {
                      key: 'tool',
                      header: 'Tool',
                      render: (row) => <code>{row.tool_id ?? '—'}</code>,
                    },
                    {
                      key: 'stage',
                      header: 'Stage',
                      width: 160,
                      render: (row) => row.stage ?? '—',
                    },
                    {
                      key: 'status',
                      header: 'Status',
                      width: 120,
                      render: (row) => (
                        <SutrBadge tone={STATE_TONE[row.status ?? ''] ?? 'neutral'}>
                          {row.status ?? 'unknown'}
                        </SutrBadge>
                      ),
                    },
                    {
                      key: 'created',
                      header: 'Raised',
                      width: 180,
                      render: (row) => formatWhen(row.created_at),
                    },
                    {
                      key: 'actions',
                      header: '',
                      width: 190,
                      render: (row) =>
                        row.status === 'pending' ? (
                          <span style={{ display: 'flex', gap: 6 }}>
                            <SutrButton
                              size="sm"
                              onClick={() => decideReview(row, 'approve')}
                              disabled={action.busy}
                            >
                              Approve
                            </SutrButton>
                            <SutrButton
                              size="sm"
                              variant="danger"
                              onClick={() => decideReview(row, 'reject')}
                              disabled={action.busy}
                            >
                              Reject
                            </SutrButton>
                          </span>
                        ) : null,
                    },
                  ]}
                  rows={reviews.data}
                  rowKey={(row) => row.id}
                  minWidth={820}
                />
              ) : (
                <SutrEmpty title="Nothing waiting for review" />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'exceptions' ? (
          <>
            <SutrCard>
              <SutrCardBody className="sutr-muted">
                Every exception expires. There is no permanent exception, and one past its date is
                not honoured even if nobody has swept it.
              </SutrCardBody>
            </SutrCard>
            {exceptions.error ? <SutrError what={exceptions.error} /> : null}
            {exceptions.data ? (
              exceptions.data.length ? (
                <SutrTable
                  columns={[
                    {
                      key: 'policy',
                      header: 'Policy',
                      render: (row) => <code>{row.policy_id ?? '—'}</code>,
                    },
                    {
                      key: 'state',
                      header: 'State',
                      width: 130,
                      render: (row) => (
                        <SutrBadge tone={STATE_TONE[row.state ?? ''] ?? 'neutral'} dot>
                          {row.state ?? 'unknown'}
                        </SutrBadge>
                      ),
                    },
                    { key: 'reason', header: 'Reason', render: (row) => row.reason ?? '' },
                    {
                      key: 'expires',
                      header: 'Expires',
                      width: 180,
                      render: (row) => formatWhen(row.expires_at),
                    },
                    {
                      key: 'actions',
                      header: '',
                      width: 190,
                      render: (row) =>
                        row.state === 'requested' || row.state === 'assessed' ? (
                          <span style={{ display: 'flex', gap: 6 }}>
                            <SutrButton
                              size="sm"
                              onClick={() => decideException(row, 'approve')}
                              disabled={action.busy}
                            >
                              Approve
                            </SutrButton>
                            <SutrButton
                              size="sm"
                              variant="danger"
                              onClick={() => decideException(row, 'reject')}
                              disabled={action.busy}
                            >
                              Reject
                            </SutrButton>
                          </span>
                        ) : null,
                    },
                  ]}
                  rows={exceptions.data}
                  rowKey={(row) => row.id}
                  minWidth={860}
                />
              ) : (
                <SutrEmpty title="No exceptions" />
              )
            ) : null}
          </>
        ) : null}
      </SutrPageBody>
    </SutrPage>
  )
}
