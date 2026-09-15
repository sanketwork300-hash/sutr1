import {
  SutrBadge,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrDefinitionList,
  SutrError,
  SutrMetric,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSectionLabel,
  SutrSpinner,
  SutrTable,
  type Column,
} from '@/components/sutr'
import { v1, type PlatformCapability, type ScalingReport } from '@/api/v1'
import { useResource } from './useResource'

/**
 * What this instance is, right now (LLD §5.4, §5.7).
 *
 * Four questions an operator asks in an incident and cannot otherwise answer
 * without a shell: which optional backends are live, may this instance write,
 * which replica is running the work that must run once, and what is still
 * per-process when there is more than one replica.
 *
 * Everything here is read-only. Changing any of it is a deployment decision —
 * an environment variable, a promotion, a restart — not a button.
 */
export default function PlatformPage() {
  const capabilities = useResource(() => v1.platform.capabilities(), [])
  const mode = useResource(() => v1.platform.mode(), [])
  const leadership = useResource(() => v1.platform.leadership(), [])
  const scaling = useResource(() => v1.platform.scaling(), [])
  const services = useResource(() => v1.platform.services(), [])

  const capabilityColumns: Column<PlatformCapability>[] = [
    { key: 'name', header: 'Capability', render: (row) => <strong>{row.name}</strong> },
    {
      key: 'available',
      header: 'State',
      width: 130,
      render: (row) => (
        <SutrBadge tone={row.available ? 'success' : 'warning'} dot>
          {row.available ? 'available' : 'degraded'}
        </SutrBadge>
      ),
    },
    {
      key: 'detail',
      header: 'What it does',
      render: (row) => (
        <>
          <div>{row.detail}</div>
          {/* Present only when unavailable: a reason on a working capability
              reads as a warning about something that is fine. */}
          {row.reason ? <div className="sutr-muted">{row.reason}</div> : null}
        </>
      ),
    },
  ]

  const scalingColumns: Column<ScalingReport['process_local_state'][number]>[] = [
    { key: 'name', header: 'State', render: (row) => <strong>{row.name}</strong> },
    {
      key: 'severity',
      header: 'Severity',
      width: 110,
      render: (row) => (
        <SutrBadge tone={row.severity === 'degraded' ? 'warning' : 'info'}>
          {row.severity}
        </SutrBadge>
      ),
    },
    {
      key: 'effect',
      header: 'With more than one replica',
      render: (row) => (
        <>
          <div>{row.effect}</div>
          <div className="sutr-muted">{row.remedy}</div>
        </>
      ),
    },
    { key: 'location', header: 'Where', render: (row) => <code>{row.location}</code> },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Operate"
        title="Platform"
        subtitle="What this instance is and what it is currently able to do. Read-only: every value here is set by deployment, not by the console."
      />

      <SutrPageBody>
        <SutrSectionLabel>This instance</SutrSectionLabel>
        {mode.error ? <SutrError what={mode.error} /> : null}
        {mode.loading || leadership.loading ? <SutrSpinner /> : null}
        {mode.data && leadership.data ? (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
              gap: 12,
            }}
          >
            <SutrMetric
              label="Mode"
              value={mode.data.mode}
              foot={mode.data.writes_allowed ? 'accepting writes' : 'reads only'}
            />
            <SutrMetric
              label="Region"
              value={mode.data.region ?? 'unset'}
              muted={!mode.data.region}
            />
            <SutrMetric
              label="Writes served by"
              value={mode.data.primary_region ?? 'not recorded'}
              muted={!mode.data.primary_region}
              foot="configured, not discovered"
            />
            <SutrMetric
              label="Jobs held here"
              value={`${leadership.data.held.length} of ${leadership.data.jobs.length}`}
              foot={leadership.data.instance_id}
            />
          </div>
        ) : null}

        {mode.data && !mode.data.writes_allowed ? (
          <SutrCard>
            <SutrCardBody>
              This is a read-only standby. Writes are refused with a 503 naming{' '}
              <strong>{mode.data.primary_region ?? 'the active region'}</strong>. These routes still
              answer because they read rather than write:{' '}
              {mode.data.read_only_writes.map((path) => (
                <code key={path} style={{ marginRight: 8 }}>
                  {path}
                </code>
              ))}
            </SutrCardBody>
          </SutrCard>
        ) : null}

        <SutrSectionLabel>Background jobs</SutrSectionLabel>
        {leadership.error ? <SutrError what={leadership.error} /> : null}
        {leadership.data ? (
          <SutrCard>
            <SutrCardHeader
              title="Who is running the work that must run once"
              meta={`lease ${leadership.data.lease_seconds}s · renewed every ${leadership.data.renew_seconds}s`}
            />
            <SutrCardBody>
              <SutrDefinitionList
                items={leadership.data.jobs.map((job) => {
                  const holder = leadership.data?.holders[job] ?? null
                  const here = leadership.data?.held.includes(job)
                  return {
                    key: job,
                    value: holder ? (
                      <span>
                        {holder} {here ? <SutrBadge tone="success">this replica</SutrBadge> : null}
                      </span>
                    ) : (
                      // No holder is a real state — the lease expired and
                      // nobody has taken it yet — and is not the same as
                      // "nobody is meant to run it".
                      <span className="sutr-muted">no live lease</span>
                    ),
                  }
                })}
              />
            </SutrCardBody>
          </SutrCard>
        ) : null}

        <SutrSectionLabel>Capabilities</SutrSectionLabel>
        {capabilities.error ? <SutrError what={capabilities.error} /> : null}
        {capabilities.loading ? <SutrSpinner /> : null}
        {capabilities.data ? (
          <SutrTable
            columns={capabilityColumns}
            rows={capabilities.data.capabilities}
            rowKey={(row) => row.name}
            minWidth={640}
          />
        ) : null}

        <SutrSectionLabel>Running more than one replica</SutrSectionLabel>
        {scaling.error ? <SutrError what={scaling.error} /> : null}
        {scaling.data ? (
          <>
            <SutrCard>
              <SutrCardBody>{scaling.data.summary}</SutrCardBody>
            </SutrCard>
            <SutrTable
              columns={scalingColumns}
              rows={scaling.data.process_local_state}
              rowKey={(row) => row.location}
              minWidth={760}
            />
          </>
        ) : null}

        <SutrSectionLabel>Service map</SutrSectionLabel>
        {services.error ? <SutrError what={services.error} /> : null}
        {services.data ? (
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
              gap: 12,
            }}
          >
            {Object.entries(services.data.planes).map(([plane, entries]) => (
              <SutrCard key={plane}>
                <SutrCardHeader title={plane} meta={`${entries.length} services`} />
                <SutrCardBody>
                  <SutrDefinitionList
                    items={entries.map((service) => ({
                      key: service.name,
                      value: (
                        <>
                          <div>{service.description}</div>
                          {service.tables.length ? (
                            <div className="sutr-muted">owns: {service.tables.join(', ')}</div>
                          ) : null}
                        </>
                      ),
                    }))}
                  />
                </SutrCardBody>
              </SutrCard>
            ))}
          </div>
        ) : null}
      </SutrPageBody>
    </SutrPage>
  )
}
