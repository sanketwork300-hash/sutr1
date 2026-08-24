import { useCallback, useEffect, useState } from 'react'
import { Activity, AlertTriangle, Clock, Timer } from 'lucide-react'
import { api, type UsageSummary } from '@/api/client'
import { formatDate, formatDateTime, formatDuration } from '@/lib/format'
import {
  SutrBarChart,
  SutrBarList,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrEmpty,
  SutrError,
  SutrMetric,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSegmented,
  describeError,
  type Point,
} from '@/components/sutr'

const RANGES = [
  { value: '7', label: '7 days' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
]

const KIND_LABEL: Record<string, string> = {
  tool_call: 'Tool calls',
  deployment_runtime: 'Deployment runtime (minutes)',
  mcp_request: 'MCP requests',
}

/**
 * Metered usage for the organisation. Every number here is what the ledger
 * recorded — a period with no activity says so instead of drawing an empty
 * chart that looks like a failure.
 */
export default function UsagePage() {
  const [days, setDays] = useState('30')
  const [summary, setSummary] = useState<UsageSummary | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async (windowDays: number) => {
    setLoading(true)
    try {
      const start = new Date(Date.now() - windowDays * 86400_000).toISOString()
      setSummary(await api.usage.summary({ start }))
      setError(null)
    } catch (err) {
      setError(describeError(err).message)
      setSummary(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(Number(days))
  }, [days, load])

  const outcome = (name: string) =>
    summary?.tool_calls_by_outcome.find((row) => row.outcome === name)?.count ?? 0

  const errored = outcome('error')
  const denied = outcome('denied')
  const points: Point[] = (summary?.daily ?? []).map((row) => ({
    date: row.date,
    label: formatDate(row.date),
    value: row.count,
  }))

  const hasUsage = Boolean(summary && summary.tool_calls > 0)

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Operate"
        title="Usage"
        subtitle={
          summary
            ? `Metered activity from ${formatDateTime(summary.start)} to ${formatDateTime(summary.end)}.`
            : 'Metered tool calls, deployment runtime and errors for this organisation.'
        }
        actions={
          <SutrSegmented ariaLabel="Time range" value={days} onChange={setDays} items={RANGES} />
        }
      />

      <SutrPageBody>
        {error ? <SutrError what="Usage could not be read." why={error} /> : null}

        <div className="sutr-grid sutr-grid--metrics">
          <SutrMetric
            label="Tool executions"
            value={loading ? null : (summary?.tool_calls ?? 0)}
            icon={<Activity size={12} />}
            muted={!hasUsage}
            foot={hasUsage ? `${outcome('executed').toLocaleString()} executed` : 'No calls yet'}
          />
          <SutrMetric
            label="Errors"
            value={loading ? null : errored}
            icon={<AlertTriangle size={12} />}
            muted={errored === 0}
            foot={
              summary && summary.tool_calls > 0
                ? `${((errored / summary.tool_calls) * 100).toFixed(1)}% of calls`
                : undefined
            }
          />
          <SutrMetric
            label="Denied by policy"
            value={loading ? null : denied}
            icon={<AlertTriangle size={12} />}
            muted={denied === 0}
            foot={denied > 0 ? 'Refused before reaching the provider' : 'Nothing refused'}
          />
          <SutrMetric
            label="Average latency"
            value={
              loading
                ? null
                : summary?.duration_ms.avg === null || summary?.duration_ms.avg === undefined
                  ? 'No data'
                  : formatDuration(summary.duration_ms.avg)
            }
            icon={<Timer size={12} />}
            foot={
              summary?.duration_ms.max !== null && summary?.duration_ms.max !== undefined
                ? `peak ${formatDuration(summary.duration_ms.max)}`
                : undefined
            }
          />
        </div>

        <SutrCard>
          <SutrCardHeader
            title="Tool executions per day"
            meta="Counted when the call reached the gateway, whatever the policy decided"
          />
          <SutrCardBody>
            {loading ? (
              <span className="sutr-skeleton" style={{ height: 132 }} />
            ) : points.length === 0 || !hasUsage ? (
              <SutrEmpty
                icon={<Clock size={17} />}
                title="No usage yet"
                body="Nothing has been metered in this period. Once an agent calls a tool, its executions appear here per day."
              />
            ) : (
              <SutrBarChart points={points} title="Executions" unit="calls" />
            )}
          </SutrCardBody>
        </SutrCard>

        <div className="sutr-split-main">
          <SutrCard>
            <SutrCardHeader title="Most used tools" meta="Top tools by execution count" />
            <SutrCardBody>
              {loading ? (
                <span className="sutr-skeleton" style={{ height: 90 }} />
              ) : (
                <SutrBarList
                  emptyLabel="No tool executions in this period."
                  items={(summary?.top_tools ?? []).map((row, index) => ({
                    key: `${row.integration_id}-${row.tool_name}-${index}`,
                    label: row.tool_name ?? 'unknown tool',
                    hint: row.integration_id ?? undefined,
                    value: row.count,
                  }))}
                />
              )}
            </SutrCardBody>
          </SutrCard>

          <SutrCard>
            <SutrCardHeader title="By provider" meta="Which integrations agents actually reach" />
            <SutrCardBody>
              {loading ? (
                <span className="sutr-skeleton" style={{ height: 90 }} />
              ) : (
                <SutrBarList
                  emptyLabel="No provider activity in this period."
                  items={(summary?.top_integrations ?? []).map((row, index) => ({
                    key: `${row.integration_id}-${index}`,
                    label: row.integration_id ?? 'unknown provider',
                    value: row.count,
                  }))}
                />
              )}
            </SutrCardBody>
          </SutrCard>
        </div>

        <div className="sutr-split-main">
          <SutrCard>
            <SutrCardHeader title="Outcomes" meta="How each call ended" />
            <SutrCardBody>
              {loading ? (
                <span className="sutr-skeleton" style={{ height: 90 }} />
              ) : (
                <SutrBarList
                  emptyLabel="No outcomes recorded in this period."
                  items={(summary?.tool_calls_by_outcome ?? []).map((row, index) => ({
                    key: `${row.outcome}-${index}`,
                    label: (row.outcome ?? 'unknown').replace(/_/g, ' '),
                    value: row.count,
                  }))}
                />
              )}
            </SutrCardBody>
          </SutrCard>

          <SutrCard>
            <SutrCardHeader title="Metered totals" meta="What the usage ledger recorded, by kind" />
            <SutrCardBody>
              {loading ? (
                <span className="sutr-skeleton" style={{ height: 90 }} />
              ) : (summary?.totals_by_kind ?? []).length === 0 ? (
                <span className="sutr-meta">Nothing metered in this period.</span>
              ) : (
                <dl className="sutr-dl">
                  {(summary?.totals_by_kind ?? []).map((row) => (
                    <div key={row.kind} style={{ display: 'contents' }}>
                      <dt className="sutr-dl__key">{KIND_LABEL[row.kind] ?? row.kind}</dt>
                      <dd className="sutr-dl__val" style={{ margin: 0 }}>
                        <span className="sutr-mono">{row.quantity.toLocaleString()}</span>
                        <span className="sutr-meta"> · {row.events.toLocaleString()} events</span>
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </SutrCardBody>
          </SutrCard>
        </div>

        <SutrCard>
          <SutrCardHeader title="By caller" meta="Where the calls came from" />
          <SutrCardBody>
            {loading ? (
              <span className="sutr-skeleton" style={{ height: 60 }} />
            ) : (
              <SutrBarList
                emptyLabel="No calls recorded in this period."
                items={(summary?.tool_calls_by_source ?? []).map((row, index) => ({
                  key: `${row.source}-${index}`,
                  label: (row.source ?? 'unknown').replace(/_/g, ' '),
                  value: row.count,
                }))}
              />
            )}
          </SutrCardBody>
        </SutrCard>
      </SutrPageBody>
    </SutrPage>
  )
}
