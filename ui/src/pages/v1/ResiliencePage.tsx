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
  SutrSectionLabel,
  SutrSpinner,
  SutrTable,
  type Column,
} from '@/components/sutr'
import { v1, type ProviderHealth, type ResiliencePolicy } from '@/api/v1'
import { useAction, useResource } from './useResource'

const SEVERITY_TONE: Record<string, 'danger' | 'warning' | 'info' | 'neutral'> = {
  P0: 'danger',
  P1: 'warning',
  P2: 'info',
  P3: 'neutral',
}

/**
 * What happens when something fails (LLD §5.8).
 *
 * Two halves. The policy is what this build was assembled with and does not
 * change at runtime. The provider table is live, and is scoped to **this
 * replica** — the circuit breaker is per process, so another replica may be
 * happily serving a provider this one has quarantined.
 */
export default function ResiliencePage() {
  const policy = useResource(() => v1.resilience.policy(), [])
  const providers = useResource(() => v1.resilience.providers(), [])
  const action = useAction()

  async function close(provider: string) {
    const ok = await action.run(
      () => v1.resilience.closeCircuit(provider),
      `Circuit for ${provider} closed on this replica.`,
    )
    if (ok) providers.reload()
  }

  const providerColumns: Column<ProviderHealth>[] = [
    { key: 'provider', header: 'Provider', render: (row) => <strong>{row.provider}</strong> },
    {
      key: 'state',
      header: 'Circuit',
      width: 120,
      render: (row) => (
        <SutrBadge
          tone={
            row.state === 'closed' ? 'success' : row.state === 'half_open' ? 'warning' : 'danger'
          }
          dot
        >
          {row.state}
        </SutrBadge>
      ),
    },
    {
      key: 'score',
      header: 'Health',
      numeric: true,
      width: 110,
      // Nothing observed is not the same as healthy, so a provider with no
      // calls shows "—", never 100.
      render: (row) =>
        row.score === null ? <span className="sutr-muted">—</span> : `${row.score}%`,
    },
    {
      key: 'counts',
      header: 'Calls',
      width: 150,
      render: (row) => (
        <span className="sutr-muted">
          {row.successes} ok · {row.failures} failed
          {row.consecutive_failures ? ` · ${row.consecutive_failures} in a row` : ''}
        </span>
      ),
    },
    {
      key: 'last_error',
      header: 'Last error',
      render: (row) => row.last_error ?? <span className="sutr-muted">—</span>,
    },
    {
      key: 'actions',
      header: '',
      width: 130,
      render: (row) =>
        row.quarantined ? (
          <SutrButton
            size="sm"
            variant="secondary"
            onClick={() => close(row.provider)}
            disabled={action.busy}
          >
            Close circuit
          </SutrButton>
        ) : null,
    },
  ]

  const domainColumns: Column<ResiliencePolicy['domains'][number]>[] = [
    { key: 'name', header: 'Domain', render: (row) => <strong>{row.name}</strong> },
    {
      key: 'severity',
      header: 'Severity',
      width: 100,
      render: (row) => (
        <SutrBadge tone={SEVERITY_TONE[row.severity] ?? 'neutral'}>{row.severity}</SutrBadge>
      ),
    },
    { key: 'effect', header: 'What breaks', render: (row) => row.effect },
    {
      key: 'survives',
      header: 'What keeps working',
      render: (row) => (
        <>
          <div>{row.survives}</div>
          <div className="sutr-muted">{row.implemented_in}</div>
        </>
      ),
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Operate"
        title="Resilience"
        subtitle="The failure-handling policy this build was assembled with, and what this replica currently thinks of each provider."
        actions={
          <SutrButton
            variant="secondary"
            onClick={() => providers.reload()}
            disabled={providers.loading}
          >
            Refresh
          </SutrButton>
        }
      />

      <SutrPageBody>
        <SutrSectionLabel>Providers</SutrSectionLabel>
        {action.error ? <SutrError what={action.error} /> : null}
        {action.message ? (
          <SutrCard>
            <SutrCardBody>{action.message}</SutrCardBody>
          </SutrCard>
        ) : null}
        {providers.error ? <SutrError what={providers.error} /> : null}
        {providers.loading ? <SutrSpinner /> : null}
        {providers.data ? (
          providers.data.providers.length ? (
            <>
              <SutrTable
                columns={providerColumns}
                rows={providers.data.providers}
                rowKey={(row) => row.provider}
                minWidth={820}
              />
              <p className="sutr-muted">
                Scope: {providers.data.scope}. The breaker is per process, so another replica may be
                serving a provider this one has quarantined.
              </p>
            </>
          ) : (
            <SutrEmpty
              title="No provider has been called yet"
              body="A provider appears here the first time this replica calls it. An empty table means no calls, not that everything is healthy."
            />
          )
        ) : null}

        <SutrSectionLabel>Policy</SutrSectionLabel>
        {policy.error ? <SutrError what={policy.error} /> : null}
        {policy.data ? (
          <>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
                gap: 12,
              }}
            >
              <SutrCard>
                <SutrCardHeader title="Timeouts" meta="seconds" />
                <SutrCardBody>
                  {Object.entries(policy.data.timeouts).map(([key, value]) => (
                    <div key={key}>
                      {key.replace(/_/g, ' ')}: <strong>{String(value)}</strong>
                    </div>
                  ))}
                </SutrCardBody>
              </SutrCard>
              <SutrCard>
                <SutrCardHeader title="Retries" />
                <SutrCardBody>
                  {Object.entries(policy.data.retries).map(([key, value]) => (
                    <div key={key}>
                      {key.replace(/_/g, ' ')}: <strong>{String(value)}</strong>
                    </div>
                  ))}
                </SutrCardBody>
              </SutrCard>
              <SutrCard>
                <SutrCardHeader title="Circuit breaker" />
                <SutrCardBody>
                  {Object.entries(policy.data.circuit_breaker).map(([key, value]) => (
                    <div key={key}>
                      {key.replace(/_/g, ' ')}: <strong>{String(value)}</strong>
                    </div>
                  ))}
                </SutrCardBody>
              </SutrCard>
            </div>

            <SutrSectionLabel>Failure domains</SutrSectionLabel>
            <SutrTable
              columns={domainColumns}
              rows={policy.data.domains}
              rowKey={(row) => row.name}
              minWidth={880}
            />

            <SutrSectionLabel>Not implemented</SutrSectionLabel>
            <SutrCard>
              <SutrCardBody>
                {Object.entries(policy.data.not_implemented).map(([name, why]) => (
                  <p key={name}>
                    <strong>{name}</strong> — {why}
                  </p>
                ))}
              </SutrCardBody>
            </SutrCard>
          </>
        ) : null}
      </SutrPageBody>
    </SutrPage>
  )
}
