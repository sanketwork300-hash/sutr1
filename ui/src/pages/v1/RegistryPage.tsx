import { useState } from 'react'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrDefinitionList,
  SutrDrawer,
  SutrEmpty,
  SutrError,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSectionLabel,
  SutrSpinner,
  SutrTable,
  SutrTabs,
  type Column,
} from '@/components/sutr'
import {
  formatWhen,
  v1,
  type ChangeRequest,
  type RegistryTool,
  type RegistryVersion,
  type TrustScore,
} from '@/api/v1'
import { useAction, useResource } from './useResource'

type Tab = 'tools' | 'changes' | 'lifecycle'

const STATE_TONE: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'neutral'> = {
  active: 'success',
  published: 'success',
  approved: 'info',
  under_review: 'warning',
  draft: 'neutral',
  deprecated: 'warning',
  suspended: 'danger',
  archived: 'neutral',
  rejected: 'danger',
}

/**
 * The system of record for tools (LLD §3.7).
 *
 * A tool's state, its immutable versions, its trust score and the change
 * requests waiting on somebody. The trust score is shown by component rather
 * than as one number, because "62" answers nothing and "documentation 0.2 of a
 * possible 1.0" answers what to do next.
 */
export default function RegistryPage() {
  const [tab, setTab] = useState<Tab>('tools')
  const [selected, setSelected] = useState<RegistryTool | null>(null)
  const action = useAction()

  const tools = useResource(() => v1.registry.tools({ limit: 200 }), [])
  const changes = useResource(() => v1.registry.changeRequests(), [])
  const lifecycle = useResource(() => v1.registry.lifecycle(), [])

  const versions = useResource<RegistryVersion[] | null>(
    () => (selected ? v1.registry.versions(selected.id) : Promise.resolve(null)),
    [selected?.id],
  )
  const trust = useResource<TrustScore | null>(
    () => (selected ? v1.registry.trust(selected.id) : Promise.resolve(null)),
    [selected?.id],
  )

  async function transition(tool: RegistryTool, state: string) {
    const ok = await action.run(
      () => v1.registry.transition(tool.id, state),
      `${tool.name} moved to ${state}.`,
    )
    if (ok) {
      tools.reload()
      setSelected(null)
    }
  }

  async function decide(request: ChangeRequest, decision: string) {
    const ok = await action.run(
      () => v1.registry.decide(request.id, decision),
      `Change request ${decision}.`,
    )
    if (ok) changes.reload()
  }

  const toolColumns: Column<RegistryTool>[] = [
    {
      key: 'name',
      header: 'Tool',
      render: (row) => (
        <>
          <strong>{row.name}</strong>
          <div className="sutr-muted">{row.tool_key}</div>
        </>
      ),
    },
    {
      key: 'state',
      header: 'State',
      width: 130,
      render: (row) => (
        <SutrBadge tone={STATE_TONE[row.lifecycle_state] ?? 'neutral'} dot>
          {row.lifecycle_state}
        </SutrBadge>
      ),
    },
    {
      key: 'version',
      header: 'Version',
      numeric: true,
      width: 90,
      render: (row) => row.current_version ?? <span className="sutr-muted">—</span>,
    },
    {
      key: 'published',
      header: 'Published',
      numeric: true,
      width: 100,
      // The list carries no trust score — it is computed on demand, and is in
      // the drawer. Showing a blank column would imply there is none.
      render: (row) => row.published_version ?? <span className="sutr-muted">not published</span>,
    },
    { key: 'updated', header: 'Updated', width: 180, render: (row) => formatWhen(row.updated_at) },
  ]

  const changeColumns: Column<ChangeRequest>[] = [
    { key: 'kind', header: 'Change', render: (row) => row.kind ?? 'change' },
    { key: 'tool', header: 'Tool', render: (row) => <code>{row.tool_id}</code> },
    {
      key: 'status',
      header: 'Status',
      width: 120,
      render: (row) => (
        <SutrBadge tone={STATE_TONE[row.status] ?? 'neutral'}>{row.status}</SutrBadge>
      ),
    },
    { key: 'created', header: 'Raised', width: 180, render: (row) => formatWhen(row.created_at) },
    {
      key: 'actions',
      header: '',
      width: 190,
      render: (row) =>
        row.status === 'pending' ? (
          <span style={{ display: 'flex', gap: 6 }}>
            <SutrButton size="sm" onClick={() => decide(row, 'approve')} disabled={action.busy}>
              Approve
            </SutrButton>
            <SutrButton
              size="sm"
              variant="danger"
              onClick={() => decide(row, 'reject')}
              disabled={action.busy}
            >
              Reject
            </SutrButton>
          </span>
        ) : null,
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Build"
        title="Registry"
        subtitle="The system of record: lifecycle state, immutable versions, trust, and the changes waiting on a decision."
        actions={
          <SutrButton variant="secondary" onClick={() => tools.reload()} disabled={tools.loading}>
            Refresh
          </SutrButton>
        }
      >
        <SutrTabs
          items={[
            { value: 'tools', label: 'Tools', count: tools.data?.length },
            { value: 'changes', label: 'Change requests', count: changes.data?.length },
            { value: 'lifecycle', label: 'Lifecycle' },
          ]}
          value={tab}
          onChange={setTab}
          ariaLabel="Registry view"
        />
      </SutrPageHeader>

      <SutrPageBody>
        {action.error ? <SutrError what={action.error} /> : null}
        {action.message ? (
          <SutrCard>
            <SutrCardBody>{action.message}</SutrCardBody>
          </SutrCard>
        ) : null}

        {tab === 'tools' ? (
          <>
            {tools.error ? <SutrError what={tools.error} /> : null}
            {tools.loading ? <SutrSpinner /> : null}
            {tools.data ? (
              tools.data.length ? (
                <SutrTable
                  columns={toolColumns}
                  rows={tools.data}
                  rowKey={(row) => row.id}
                  onRowClick={setSelected}
                  minWidth={760}
                />
              ) : (
                <SutrEmpty
                  title="No tools registered"
                  body="A tool enters the registry when it is registered from a compiled API or an MCP server."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'changes' ? (
          <>
            {changes.error ? <SutrError what={changes.error} /> : null}
            {changes.data ? (
              changes.data.length ? (
                <SutrTable
                  columns={changeColumns}
                  rows={changes.data}
                  rowKey={(row) => row.id}
                  minWidth={760}
                />
              ) : (
                <SutrEmpty
                  title="Nothing waiting"
                  body="Changes that need a second pair of eyes appear here."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'lifecycle' ? (
          <>
            {lifecycle.error ? <SutrError what={lifecycle.error} /> : null}
            {lifecycle.data ? (
              <SutrCard>
                <SutrCardHeader title="States and the transitions between them" />
                <SutrCardBody>
                  <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                    {JSON.stringify(lifecycle.data, null, 2)}
                  </pre>
                </SutrCardBody>
              </SutrCard>
            ) : null}
          </>
        ) : null}
      </SutrPageBody>

      <SutrDrawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.name ?? ''}
        subtitle={selected?.tool_key}
        wide
        actions={
          selected ? (
            <span style={{ display: 'flex', gap: 6 }}>
              <SutrButton
                size="sm"
                onClick={() => transition(selected, 'published')}
                disabled={action.busy}
              >
                Publish
              </SutrButton>
              <SutrButton
                size="sm"
                variant="secondary"
                onClick={() => transition(selected, 'deprecated')}
                disabled={action.busy}
              >
                Deprecate
              </SutrButton>
            </span>
          ) : null
        }
      >
        {selected ? (
          <>
            <SutrDefinitionList
              items={[
                { key: 'State', value: selected.lifecycle_state },
                { key: 'Visibility', value: selected.visibility ?? '—' },
                { key: 'Integration', value: selected.integration_id ?? '—' },
                { key: 'Created', value: formatWhen(selected.created_at) },
              ]}
            />

            <SutrSectionLabel>Trust</SutrSectionLabel>
            {trust.loading ? <SutrSpinner /> : null}
            {trust.error ? <SutrError what={trust.error} /> : null}
            {trust.data ? (
              trust.data.components?.length ? (
                <SutrTable
                  columns={[
                    { key: 'name', header: 'Component', render: (row) => row.name },
                    {
                      key: 'weight',
                      header: 'Weight',
                      numeric: true,
                      width: 90,
                      render: (row) => row.weight,
                    },
                    {
                      key: 'points',
                      header: 'Points',
                      numeric: true,
                      width: 100,
                      // A component with no evidence is *unavailable*, not zero
                      // — the difference between "we checked and it is bad" and
                      // "we could not check".
                      render: (row) =>
                        row.available ? (
                          row.points
                        ) : (
                          <span className="sutr-muted">unavailable</span>
                        ),
                    },
                    {
                      key: 'reason',
                      header: 'Why',
                      render: (row) => row.unavailable_reason ?? '',
                    },
                  ]}
                  rows={trust.data.components}
                  rowKey={(row) => row.name}
                  minWidth={520}
                />
              ) : (
                <p className="sutr-muted">No trust components were returned for this tool.</p>
              )
            ) : null}

            <SutrSectionLabel>Versions</SutrSectionLabel>
            {versions.data?.length ? (
              <SutrTable
                columns={[
                  {
                    key: 'version',
                    header: 'Version',
                    numeric: true,
                    width: 90,
                    render: (row) => row.version,
                  },
                  { key: 'state', header: 'State', width: 120, render: (row) => row.state ?? '—' },
                  {
                    key: 'created',
                    header: 'Created',
                    width: 180,
                    render: (row) => formatWhen(row.created_at),
                  },
                  { key: 'notes', header: 'Notes', render: (row) => row.notes ?? '' },
                ]}
                rows={versions.data}
                rowKey={(row) => String(row.version)}
                minWidth={560}
              />
            ) : (
              <p className="sutr-muted">No versions cut yet.</p>
            )}
          </>
        ) : null}
      </SutrDrawer>
    </SutrPage>
  )
}
