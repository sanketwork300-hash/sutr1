import { useState } from 'react'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSearchInput,
  SutrSectionLabel,
  SutrSegmented,
  SutrSpinner,
  SutrTable,
  type Column,
} from '@/components/sutr'
import { formatWhen, v1, type OutboxEventRow } from '@/api/v1'
import { useAction, useResource } from './useResource'

const STATE_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
  published: 'success',
  pending: 'warning',
  dead_lettered: 'danger',
}

/**
 * The transactional outbox (LLD §5.6).
 *
 * Every event is written in the same transaction as the change it describes,
 * so this table is the answer to "did that actually happen?" — and a bus that
 * is down shows as a queue that is long, not as facts that are missing.
 */
export default function EventsPage() {
  const [view, setView] = useState<'all' | 'dead'>('all')
  const [correlationId, setCorrelationId] = useState('')
  const action = useAction()

  const events = useResource(
    () =>
      view === 'dead'
        ? v1.events.deadLetter()
        : v1.events.list({ correlation_id: correlationId || undefined, limit: 200 }),
    [view, correlationId],
  )

  async function retry(eventId: string) {
    const ok = await action.run(() => v1.events.retry(eventId), 'Event queued for another attempt.')
    if (ok) events.reload()
  }

  const columns: Column<OutboxEventRow>[] = [
    { key: 'event_type', header: 'Event', render: (row) => <strong>{row.event_type}</strong> },
    {
      key: 'state',
      header: 'State',
      width: 130,
      render: (row) => (
        <SutrBadge tone={STATE_TONE[row.state] ?? 'neutral'} dot>
          {row.state}
        </SutrBadge>
      ),
    },
    {
      key: 'attempts',
      header: 'Attempts',
      numeric: true,
      width: 90,
      render: (row) => row.attempts ?? 0,
    },
    {
      key: 'created_at',
      header: 'Recorded',
      width: 190,
      render: (row) => formatWhen(row.created_at),
    },
    {
      key: 'published_at',
      header: 'Published',
      width: 190,
      render: (row) =>
        row.published_at ? formatWhen(row.published_at) : <span className="sutr-muted">—</span>,
    },
    {
      key: 'correlation_id',
      header: 'Correlation',
      render: (row) =>
        row.correlation_id ? (
          <code>{row.correlation_id}</code>
        ) : (
          <span className="sutr-muted">—</span>
        ),
    },
    {
      key: 'last_error',
      header: 'Last error',
      render: (row) => row.last_error ?? '',
    },
    {
      key: 'actions',
      header: '',
      width: 100,
      render: (row) =>
        row.state === 'dead_lettered' ? (
          <SutrButton
            size="sm"
            variant="secondary"
            onClick={() => retry(row.event_id)}
            disabled={action.busy}
          >
            Retry
          </SutrButton>
        ) : null,
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Operate"
        title="Events"
        subtitle="The transactional outbox. Events are recorded in the same transaction as the change they describe, so a bus that is down delays delivery rather than losing facts."
        actions={
          <SutrSegmented
            items={[
              { value: 'all', label: 'All' },
              { value: 'dead', label: 'Dead letter' },
            ]}
            value={view}
            onChange={setView}
            ariaLabel="Which events"
          />
        }
      />

      <SutrPageBody>
        {view === 'all' ? (
          <SutrSearchInput
            value={correlationId}
            onValueChange={setCorrelationId}
            placeholder="Filter by correlation id"
            ariaLabel="Correlation id"
            maxWidth={360}
          />
        ) : (
          <SutrCard>
            <SutrCardBody>
              These events exhausted their retries. Nothing will try them again on its own — that is
              what the state means.
            </SutrCardBody>
          </SutrCard>
        )}

        {action.error ? <SutrError what={action.error} /> : null}
        {action.message ? (
          <SutrCard>
            <SutrCardBody>{action.message}</SutrCardBody>
          </SutrCard>
        ) : null}

        <SutrSectionLabel>
          {view === 'dead' ? 'Dead-lettered events' : 'Recent events'}
        </SutrSectionLabel>
        {events.error ? <SutrError what={events.error} /> : null}
        {events.loading ? <SutrSpinner /> : null}
        {events.data ? (
          events.data.length ? (
            <SutrTable
              columns={columns}
              rows={events.data}
              rowKey={(row) => row.event_id}
              minWidth={1100}
            />
          ) : (
            <SutrEmpty
              title={view === 'dead' ? 'Nothing has been dead-lettered' : 'No events'}
              body={
                view === 'dead'
                  ? 'Every event that has been produced was published, or is still waiting.'
                  : 'Events appear here as soon as anything writes one.'
              }
            />
          )
        ) : null}
      </SutrPageBody>
    </SutrPage>
  )
}
