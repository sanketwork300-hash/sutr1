import { useState } from 'react'
import { Search } from 'lucide-react'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrEmpty,
  SutrError,
  SutrInput,
  SutrMetric,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSectionLabel,
  SutrSpinner,
  SutrTable,
  describeError,
  type Column,
} from '@/components/sutr'
import { v1, type DiscoveryResult, type DiscoveryStage } from '@/api/v1'
import { useResource } from './useResource'

function scoreOf(row: Record<string, unknown>): string {
  const score = row.score
  return typeof score === 'number' ? score.toFixed(3) : '—'
}

function nameOf(row: Record<string, unknown>): string {
  const candidate = row.candidate as Record<string, unknown> | undefined
  return (
    (candidate?.name as string) ??
    (candidate?.tool_key as string) ??
    (row.tool_id as string) ??
    'unnamed'
  )
}

/**
 * Intent to ranked tools (LLD §3.8).
 *
 * The results are the smaller half of what this page is for. The larger half
 * is everything underneath them: which stage spent the time, which rankers
 * were unavailable, and what the engine did instead — because "no results" and
 * "results, ranked by keyword because the vector index was down" look
 * identical until something says so.
 */
export default function DiscoveryPage() {
  const [intent, setIntent] = useState('')
  const [result, setResult] = useState<DiscoveryResult | null>(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const capabilities = useResource(() => v1.discovery.capabilities(), [])

  async function search(event?: React.FormEvent) {
    event?.preventDefault()
    if (!intent.trim()) return
    setSearching(true)
    setError(null)
    try {
      setResult(await v1.discovery.search({ intent: intent.trim(), limit: 10 }))
    } catch (caught) {
      setError(describeError(caught).message)
      setResult(null)
    } finally {
      setSearching(false)
    }
  }

  const stageColumns: Column<DiscoveryStage>[] = [
    { key: 'name', header: 'Stage', render: (row) => <strong>{row.name}</strong> },
    {
      key: 'duration',
      header: 'Took',
      numeric: true,
      width: 110,
      render: (row) => `${row.duration_ms.toFixed(1)} ms`,
    },
    {
      key: 'detail',
      header: 'Detail',
      render: (row) => (
        <span className="sutr-muted">
          {Object.entries(row.detail ?? {})
            .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
            .join(' · ')}
        </span>
      ),
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="Discovery"
        subtitle="Ask for a tool the way an agent would, and see what the engine did to answer — including what it skipped."
      />

      <SutrPageBody>
        <SutrCard>
          <SutrCardBody>
            <form onSubmit={search} style={{ display: 'flex', gap: 8 }}>
              <SutrInput
                value={intent}
                onChange={(event) => setIntent(event.target.value)}
                placeholder="refund a payment for a customer"
                aria-label="Intent"
                style={{ flex: 1 }}
              />
              <SutrButton type="submit" disabled={searching || !intent.trim()}>
                <Search size={14} /> Search
              </SutrButton>
            </form>
          </SutrCardBody>
        </SutrCard>

        {error ? <SutrError what={error} /> : null}
        {searching ? <SutrSpinner /> : null}

        {result ? (
          <>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
                gap: 12,
              }}
            >
              <SutrMetric label="Results" value={result.results.length} />
              <SutrMetric
                label="Latency"
                value={result.latency_ms === undefined ? '—' : `${result.latency_ms.toFixed(1)} ms`}
                foot={result.cached ? 'served from cache' : 'computed'}
              />
              <SutrMetric label="Ranking" value={result.ranking_version ?? '—'} />
              <SutrMetric
                label="Stale listings"
                value={result.stale_results ?? 0}
                muted={!result.stale_results}
                foot="projected before the registry last changed"
              />
            </div>

            {result.degradations.length ? (
              <SutrCard>
                <SutrCardHeader title="What the engine could not do" />
                <SutrCardBody>
                  {result.degradations.map((degradation) => (
                    <p key={`${degradation.stage}-${degradation.reason}`}>
                      <SutrBadge tone="warning">{degradation.stage}</SutrBadge> {degradation.reason}{' '}
                      <span className="sutr-muted">{degradation.effect}</span>
                    </p>
                  ))}
                </SutrCardBody>
              </SutrCard>
            ) : null}

            <SutrSectionLabel>Ranked tools</SutrSectionLabel>
            {result.results.length ? (
              <SutrTable
                columns={[
                  { key: 'name', header: 'Tool', render: (row) => <strong>{nameOf(row)}</strong> },
                  {
                    key: 'score',
                    header: 'Score',
                    numeric: true,
                    width: 100,
                    render: (row) => scoreOf(row),
                  },
                  {
                    key: 'why',
                    header: 'Why',
                    render: (row) => (
                      <span className="sutr-muted">
                        {row.reasons ? JSON.stringify(row.reasons) : '—'}
                      </span>
                    ),
                  },
                ]}
                rows={result.results}
                rowKey={(row) => String(row.tool_id ?? nameOf(row))}
                minWidth={640}
              />
            ) : (
              <SutrEmpty
                title="Nothing matched"
                body={
                  result.suggestions?.length
                    ? `The policy filter excluded some candidates: ${JSON.stringify(result.suggestions)}`
                    : 'No published tool matched this intent for this organisation.'
                }
              />
            )}

            <SutrSectionLabel>Where the time went</SutrSectionLabel>
            <SutrTable
              columns={stageColumns}
              rows={result.stages}
              rowKey={(row) => row.name}
              minWidth={560}
            />
          </>
        ) : null}

        {capabilities.data ? (
          <>
            <SutrSectionLabel>Retrieval capabilities</SutrSectionLabel>
            <SutrCard>
              <SutrCardBody>
                <pre className="sutr-muted" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                  {JSON.stringify(capabilities.data, null, 2)}
                </pre>
              </SutrCardBody>
            </SutrCard>
          </>
        ) : null}
      </SutrPageBody>
    </SutrPage>
  )
}
