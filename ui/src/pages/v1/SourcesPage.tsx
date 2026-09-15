import { useState } from 'react'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrDrawer,
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
import { formatWhen, v1, type ApiSource, type DriftReport } from '@/api/v1'
import { useAction, useResource } from './useResource'

const CLASSIFICATION_TONE: Record<string, 'danger' | 'warning' | 'info' | 'neutral'> = {
  breaking: 'danger',
  additive: 'info',
  cosmetic: 'neutral',
  unknown: 'warning',
}

/**
 * Where API definitions live, and what changed there (LLD §3.3).
 *
 * The drift half is the interesting one: a source that changed is not a
 * problem, a source that changed in a *breaking* way is, and the two are shown
 * as different things rather than as one "out of date" badge.
 */
export default function SourcesPage() {
  const [selected, setSelected] = useState<ApiSource | null>(null)
  const action = useAction()

  const sources = useResource(() => v1.sources.list(), [])
  const connectors = useResource(() => v1.sources.connectors(), [])
  const drift = useResource<DriftReport[] | null>(
    () => (selected ? v1.sources.drift(selected.id) : Promise.resolve(null)),
    [selected?.id],
  )

  async function check(source: ApiSource) {
    const ok = await action.run(() => v1.sources.check(source.id), 'Source checked.')
    if (ok) {
      sources.reload()
      drift.reload()
    }
  }

  async function applyDrift(report: DriftReport) {
    const ok = await action.run(() => v1.sources.applyDrift(report.id), 'Drift applied.')
    if (ok) drift.reload()
  }

  async function dismissDrift(report: DriftReport) {
    const ok = await action.run(() => v1.sources.dismissDrift(report.id), 'Drift dismissed.')
    if (ok) drift.reload()
  }

  const columns: Column<ApiSource>[] = [
    {
      key: 'label',
      header: 'Source',
      render: (row) => (
        <>
          <strong>{row.label || row.id}</strong>
          <div className="sutr-muted">{row.connector}</div>
        </>
      ),
    },
    { key: 'role', header: 'Role', width: 120, render: (row) => row.role ?? '—' },
    {
      key: 'status',
      header: 'Status',
      width: 120,
      render: (row) => (
        <SutrBadge tone={row.last_error ? 'danger' : 'success'} dot>
          {row.last_error ? 'failing' : (row.status ?? 'ok')}
        </SutrBadge>
      ),
    },
    {
      key: 'checked',
      header: 'Last checked',
      width: 190,
      render: (row) =>
        row.last_checked_at ? (
          formatWhen(row.last_checked_at)
        ) : (
          <span className="sutr-muted">never</span>
        ),
    },
    { key: 'error', header: 'Last error', render: (row) => row.last_error ?? '' },
    {
      key: 'actions',
      header: '',
      width: 100,
      render: (row) => (
        <SutrButton
          size="sm"
          variant="secondary"
          onClick={(event) => {
            event.stopPropagation()
            check(row)
          }}
          disabled={action.busy}
        >
          Check
        </SutrButton>
      ),
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="Sources"
        subtitle="Where API definitions come from, when each was last checked, and what changed since."
        actions={
          <SutrButton
            variant="secondary"
            onClick={() => sources.reload()}
            disabled={sources.loading}
          >
            Refresh
          </SutrButton>
        }
      />

      <SutrPageBody>
        {action.error ? <SutrError what={action.error} /> : null}
        {action.message ? (
          <SutrCard>
            <SutrCardBody>{action.message}</SutrCardBody>
          </SutrCard>
        ) : null}

        {sources.error ? <SutrError what={sources.error} /> : null}
        {sources.loading ? <SutrSpinner /> : null}
        {sources.data ? (
          sources.data.length ? (
            <SutrTable
              columns={columns}
              rows={sources.data}
              rowKey={(row) => row.id}
              onRowClick={setSelected}
              minWidth={860}
            />
          ) : (
            <SutrEmpty
              title="No sources connected"
              body="A source is a place definitions live — a repository, a spec registry, a gateway — that this platform polls rather than being pushed to."
            />
          )
        ) : null}

        {connectors.data ? (
          <>
            <SutrSectionLabel>Available connectors</SutrSectionLabel>
            <SutrCard>
              <SutrCardBody>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  {connectors.data.map((connector) => (
                    <SutrBadge key={String(connector.name ?? connector.id)}>
                      {String(connector.label ?? connector.name ?? connector.id)}
                    </SutrBadge>
                  ))}
                </div>
              </SutrCardBody>
            </SutrCard>
          </>
        ) : null}
      </SutrPageBody>

      <SutrDrawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.label || selected?.id || ''}
        subtitle={selected?.connector}
        wide
      >
        <SutrSectionLabel>Drift</SutrSectionLabel>
        {drift.loading ? <SutrSpinner /> : null}
        {drift.error ? <SutrError what={drift.error} /> : null}
        {drift.data ? (
          drift.data.length ? (
            <SutrTable
              columns={[
                {
                  key: 'classification',
                  header: 'Change',
                  width: 130,
                  render: (row) => (
                    <SutrBadge
                      tone={CLASSIFICATION_TONE[row.classification ?? 'unknown'] ?? 'neutral'}
                    >
                      {row.classification ?? 'unclassified'}
                    </SutrBadge>
                  ),
                },
                { key: 'summary', header: 'Summary', render: (row) => row.summary ?? '' },
                {
                  key: 'when',
                  header: 'Detected',
                  width: 180,
                  render: (row) => formatWhen(row.created_at),
                },
                {
                  key: 'actions',
                  header: '',
                  width: 180,
                  render: (row) => (
                    <span style={{ display: 'flex', gap: 6 }}>
                      <SutrButton size="sm" onClick={() => applyDrift(row)} disabled={action.busy}>
                        Apply
                      </SutrButton>
                      <SutrButton
                        size="sm"
                        variant="secondary"
                        onClick={() => dismissDrift(row)}
                        disabled={action.busy}
                      >
                        Dismiss
                      </SutrButton>
                    </span>
                  ),
                },
              ]}
              rows={drift.data}
              rowKey={(row) => row.id}
              minWidth={720}
            />
          ) : (
            <SutrEmpty
              title="No drift recorded"
              body="Either nothing has changed upstream, or this source has not been checked since it did."
            />
          )
        ) : null}
      </SutrDrawer>
    </SutrPage>
  )
}
