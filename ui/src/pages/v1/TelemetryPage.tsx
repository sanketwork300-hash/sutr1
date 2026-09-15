import { useState } from 'react'
import {
  SutrBadge,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrMetric,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSectionLabel,
  SutrSegmented,
  SutrSpinner,
  SutrTable,
  type Column,
} from '@/components/sutr'
import {
  formatDuration,
  formatWhen,
  v1,
  type LatencySummary,
  type TelemetryOperation,
  type TelemetryTimeline,
} from '@/api/v1'
import { useResource } from './useResource'

const WINDOWS = [
  { value: '1', label: '1 hour' },
  { value: '24', label: '24 hours' },
  { value: '168', label: '7 days' },
  { value: '720', label: '30 days' },
]

function Latency({ latency }: { latency: LatencySummary }) {
  if (latency.count === 0) return <span className="sutr-muted">no timed calls</span>
  return (
    <span>
      p50 {latency.p50} ms · p95 {latency.p95} ms · p99 {latency.p99} ms · max {latency.max} ms
    </span>
  )
}

/**
 * This organisation's own telemetry (LLD §5.3).
 *
 * Read from this platform's own tables, scoped to the calling organisation by
 * the query itself. Prometheus, Loki and Tempo hold every tenant's data and
 * are never exposed here — the trace id on an operation is the seam: an
 * operator holding both can open the trace.
 */
export default function TelemetryPage() {
  const [window, setWindow] = useState('24')
  const [openId, setOpenId] = useState<string | null>(null)

  const hours = Number(window)
  const summary = useResource(() => v1.observability.summary(hours), [hours])
  const operations = useResource(() => v1.observability.operations(hours, 100), [hours])
  const capabilities = useResource(() => v1.observability.capabilities(), [])
  const timeline = useResource<TelemetryTimeline | null>(
    () => (openId ? v1.observability.operation(openId) : Promise.resolve(null)),
    [openId],
  )

  const operationColumns: Column<TelemetryOperation>[] = [
    {
      key: 'correlation_id',
      header: 'Operation',
      render: (row) => <code>{row.correlation_id}</code>,
    },
    { key: 'steps', header: 'Steps', numeric: true, width: 80, render: (row) => row.steps },
    {
      key: 'duration',
      header: 'Duration',
      numeric: true,
      width: 110,
      render: (row) => formatDuration(row.duration_ms),
    },
    { key: 'started', header: 'Started', width: 190, render: (row) => formatWhen(row.started_at) },
    {
      key: 'trace',
      header: 'Trace',
      width: 120,
      // Absent when the call was made with tracing off, which is the default
      // install. Showing a blank is honest; inventing an id is not.
      render: (row) =>
        row.trace_id ? (
          <code title={row.trace_id}>{row.trace_id.slice(0, 12)}…</code>
        ) : (
          <span className="sutr-muted">not traced</span>
        ),
    },
  ]

  const providerRows = Object.entries(summary.data?.by_provider ?? {}).map(([provider, value]) => ({
    provider,
    ...value,
  }))

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Operate"
        title="Telemetry"
        subtitle="Call rate, errors and latency for this organisation, and the timeline of any one operation."
        actions={
          <SutrSegmented
            items={WINDOWS}
            value={window}
            onChange={setWindow}
            ariaLabel="Time window"
          />
        }
      />

      <SutrPageBody>
        {summary.error ? <SutrError what={summary.error} /> : null}
        {summary.loading ? <SutrSpinner /> : null}

        {summary.data ? (
          <>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
                gap: 12,
              }}
            >
              <SutrMetric label="Calls" value={summary.data.calls} />
              <SutrMetric
                label="Errors"
                value={summary.data.errors}
                foot={`${(summary.data.error_ratio * 100).toFixed(2)}% of calls`}
              />
              <SutrMetric label="Calls / minute" value={summary.data.calls_per_minute} />
              <SutrMetric
                label="p95 latency"
                value={
                  summary.data.latency_ms.p95 === null ? '—' : `${summary.data.latency_ms.p95} ms`
                }
                foot={`over ${summary.data.latency_ms.count} timed calls`}
              />
            </div>

            {summary.data.truncated ? (
              <SutrCard>
                <SutrCardBody>
                  This window held more calls than are read in one summary, so the numbers above
                  describe the most recent {summary.data.calls} of them rather than the whole
                  period.
                </SutrCardBody>
              </SutrCard>
            ) : null}

            <SutrSectionLabel>By outcome</SutrSectionLabel>
            <SutrCard>
              <SutrCardBody>
                {Object.keys(summary.data.by_outcome).length === 0 ? (
                  <span className="sutr-muted">No calls in this window.</span>
                ) : (
                  Object.entries(summary.data.by_outcome).map(([outcome, count]) => (
                    <SutrBadge
                      key={outcome}
                      tone={
                        outcome === 'executed'
                          ? 'success'
                          : outcome === 'error'
                            ? 'danger'
                            : 'neutral'
                      }
                    >
                      {outcome}: {count}
                    </SutrBadge>
                  ))
                )}
              </SutrCardBody>
            </SutrCard>

            <SutrSectionLabel>By provider</SutrSectionLabel>
            {providerRows.length ? (
              <SutrTable
                columns={[
                  {
                    key: 'provider',
                    header: 'Provider',
                    render: (row) => <strong>{row.provider}</strong>,
                  },
                  {
                    key: 'calls',
                    header: 'Calls',
                    numeric: true,
                    width: 90,
                    render: (row) => row.calls,
                  },
                  {
                    key: 'latency',
                    header: 'Latency',
                    render: (row) => <Latency latency={row.latency_ms} />,
                  },
                ]}
                rows={providerRows}
                rowKey={(row) => row.provider}
                minWidth={560}
              />
            ) : (
              <SutrEmpty title="No provider calls in this window" />
            )}
          </>
        ) : null}

        <SutrSectionLabel>Operations</SutrSectionLabel>
        {operations.error ? <SutrError what={operations.error} /> : null}
        {operations.data ? (
          operations.data.operations.length ? (
            <SutrTable
              columns={operationColumns}
              rows={operations.data.operations}
              rowKey={(row) => row.correlation_id}
              onRowClick={(row) => setOpenId(row.correlation_id)}
              minWidth={760}
            />
          ) : (
            <SutrEmpty
              title="No operations in this window"
              body="An operation is everything that shared one correlation id — usually one request, sometimes an approval granted and then executed."
            />
          )
        ) : null}

        {capabilities.data ? (
          <SutrCard>
            <SutrCardHeader title="What this telemetry is, and is not" />
            <SutrCardBody>
              <p className="sutr-muted">
                {String(
                  (capabilities.data.tenant_telemetry as Record<string, unknown> | undefined)
                    ?.detail ?? '',
                )}
              </p>
            </SutrCardBody>
          </SutrCard>
        ) : null}
      </SutrPageBody>

      <SutrDrawer
        open={openId !== null}
        onClose={() => setOpenId(null)}
        title="Operation timeline"
        subtitle={openId ?? undefined}
        wide
      >
        {timeline.loading ? <SutrSpinner /> : null}
        {timeline.error ? <SutrError what={timeline.error} /> : null}
        {timeline.data ? (
          <>
            {timeline.data.trace_id ? (
              <p>
                Trace <code>{timeline.data.trace_id}</code> — open this id in the trace backend for
                the spans.
              </p>
            ) : (
              <p className="sutr-muted">
                No trace was recorded for this operation: tracing is off by default.
              </p>
            )}
            <SutrTable
              columns={[
                {
                  key: 'timestamp',
                  header: 'When',
                  width: 190,
                  render: (row) => formatWhen(row.timestamp),
                },
                { key: 'provider', header: 'Provider', render: (row) => row.provider_id ?? '—' },
                { key: 'tool', header: 'Tool', render: (row) => row.tool_id ?? '—' },
                {
                  key: 'outcome',
                  header: 'Outcome',
                  width: 110,
                  render: (row) => (
                    <SutrBadge tone={row.outcome === 'executed' ? 'success' : 'warning'}>
                      {row.outcome ?? 'unknown'}
                    </SutrBadge>
                  ),
                },
                {
                  key: 'duration',
                  header: 'Duration',
                  numeric: true,
                  width: 100,
                  render: (row) => formatDuration(row.duration_ms),
                },
                { key: 'error', header: 'Error', render: (row) => row.error ?? '' },
              ]}
              rows={timeline.data.steps}
              rowKey={(row) => `${row.timestamp}-${row.tool_id ?? ''}`}
              minWidth={780}
            />
          </>
        ) : null}
      </SutrDrawer>
    </SutrPage>
  )
}
