import { useState } from 'react'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrDefinitionList,
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
import { formatWhen, v1, type AccessPass, type AgentIdentity } from '@/api/v1'
import { useAction, useResource } from './useResource'

type Tab = 'agents' | 'passes' | 'rules'

/**
 * Agent identity and scoped access (LLD §4.3).
 *
 * An agent is a first-class identity here, not a user with a shared key: it
 * can be revoked on its own, and a pass issued to it is scoped and expires.
 * Revocation is shown as a state rather than a deletion, because a revoked
 * agent that made calls yesterday still has to be explicable today.
 */
export default function AgentsPage() {
  const [tab, setTab] = useState<Tab>('agents')
  const action = useAction()

  const agents = useResource(() => v1.provisioning.agents(), [])
  const passes = useResource(() => v1.provisioning.passes(), [])
  const rules = useResource(() => v1.provisioning.rules(), [])
  const whoami = useResource(() => v1.provisioning.whoami(), [])
  const capabilities = useResource(() => v1.provisioning.capabilities(), [])

  async function revokeAgent(agent: AgentIdentity) {
    const ok = await action.run(
      () => v1.provisioning.revokeAgent(agent.id, 'revoked from the console'),
      `${agent.name} revoked.`,
    )
    if (ok) agents.reload()
  }

  async function revokePass(pass: AccessPass) {
    const ok = await action.run(
      () => v1.provisioning.revokePass(pass.id, 'revoked from the console'),
      'Access pass revoked.',
    )
    if (ok) passes.reload()
  }

  const agentColumns: Column<AgentIdentity>[] = [
    {
      key: 'name',
      header: 'Agent',
      render: (row) => (
        <>
          <strong>{row.name}</strong>
          <div className="sutr-muted">{row.id}</div>
        </>
      ),
    },
    { key: 'kind', header: 'Kind', width: 140, render: (row) => row.kind ?? '—' },
    {
      key: 'status',
      header: 'Status',
      width: 120,
      render: (row) => (
        <SutrBadge tone={row.revoked_at ? 'danger' : row.active ? 'success' : 'neutral'} dot>
          {row.revoked_at ? 'revoked' : row.active ? 'active' : 'inactive'}
        </SutrBadge>
      ),
    },
    {
      key: 'seen',
      header: 'Last seen',
      width: 180,
      // Never seen is a fact about an identity that has been issued and not
      // used, which is worth knowing before revoking it.
      render: (row) =>
        row.last_seen_at ? formatWhen(row.last_seen_at) : <span className="sutr-muted">never</span>,
    },
    { key: 'created', header: 'Created', width: 180, render: (row) => formatWhen(row.created_at) },
    {
      key: 'actions',
      header: '',
      width: 110,
      render: (row) =>
        row.revoked_at ? null : (
          <SutrButton
            size="sm"
            variant="danger"
            onClick={() => revokeAgent(row)}
            disabled={action.busy}
          >
            Revoke
          </SutrButton>
        ),
    },
  ]

  const passColumns: Column<AccessPass>[] = [
    { key: 'agent', header: 'Agent', render: (row) => <code>{row.agent_id ?? '—'}</code> },
    {
      key: 'scope',
      header: 'Scope',
      render: (row) => (
        <>
          <div>{row.resource ?? '—'}</div>
          {/* The tools, not "all tools on this integration": a pass names them. */}
          <div className="sutr-muted">{(row.tools ?? []).join(', ') || 'no tools named'}</div>
        </>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      width: 120,
      render: (row) => (
        <SutrBadge
          tone={row.revoked_at ? 'danger' : row.status === 'active' ? 'success' : 'neutral'}
          dot
        >
          {row.revoked_at ? 'revoked' : (row.status ?? 'unknown')}
        </SutrBadge>
      ),
    },
    {
      key: 'used',
      header: 'Used',
      numeric: true,
      width: 90,
      render: (row) => row.use_count ?? 0,
    },
    { key: 'expires', header: 'Expires', width: 180, render: (row) => formatWhen(row.expires_at) },
    {
      key: 'actions',
      header: '',
      width: 110,
      render: (row) =>
        row.revoked_at ? null : (
          <SutrButton
            size="sm"
            variant="danger"
            onClick={() => revokePass(row)}
            disabled={action.busy}
          >
            Revoke
          </SutrButton>
        ),
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Govern"
        title="Agents"
        subtitle="Agent identities, the scoped passes issued to them, and the rules that decide what any of them may reach."
      >
        <SutrTabs
          items={[
            { value: 'agents', label: 'Identities', count: agents.data?.length },
            { value: 'passes', label: 'Access passes', count: passes.data?.length },
            { value: 'rules', label: 'Rules', count: rules.data?.length },
          ]}
          value={tab}
          onChange={setTab}
          ariaLabel="Provisioning view"
        />
      </SutrPageHeader>

      <SutrPageBody>
        {whoami.data ? (
          <SutrCard>
            <SutrCardHeader title="This session, as the platform sees it" />
            <SutrCardBody>
              <SutrDefinitionList
                items={Object.entries(whoami.data).map(([key, value]) => ({
                  key: key.replace(/_/g, ' '),
                  value: typeof value === 'object' ? JSON.stringify(value) : String(value ?? '—'),
                }))}
              />
            </SutrCardBody>
          </SutrCard>
        ) : null}

        {action.error ? <SutrError what={action.error} /> : null}
        {action.message ? (
          <SutrCard>
            <SutrCardBody>{action.message}</SutrCardBody>
          </SutrCard>
        ) : null}

        {tab === 'agents' ? (
          <>
            {agents.error ? <SutrError what={agents.error} /> : null}
            {agents.loading ? <SutrSpinner /> : null}
            {agents.data ? (
              agents.data.length ? (
                <SutrTable
                  columns={agentColumns}
                  rows={agents.data}
                  rowKey={(row) => row.id}
                  minWidth={760}
                />
              ) : (
                <SutrEmpty
                  title="No agent identities"
                  body="An agent identity is separate from a user and from an API key: it can be revoked on its own without taking a human's access with it."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'passes' ? (
          <>
            {passes.error ? <SutrError what={passes.error} /> : null}
            {passes.data ? (
              passes.data.length ? (
                <SutrTable
                  columns={passColumns}
                  rows={passes.data}
                  rowKey={(row) => row.id}
                  minWidth={860}
                />
              ) : (
                <SutrEmpty
                  title="No access passes"
                  body="A pass is scoped to a tool and expires. Issuing one is how an agent gets access to something without being given a standing grant."
                />
              )
            ) : null}
          </>
        ) : null}

        {tab === 'rules' ? (
          <>
            {rules.error ? <SutrError what={rules.error} /> : null}
            {rules.data ? (
              rules.data.length ? (
                <SutrTable
                  columns={[
                    {
                      key: 'rule',
                      header: 'Rule',
                      render: (row) => (
                        <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                          {JSON.stringify(row, null, 2)}
                        </pre>
                      ),
                    },
                  ]}
                  rows={rules.data}
                  // SutrTable passes the row only, so an index would always be
                  // zero and every key would collide. The rule's own id when it
                  // has one, its content when it does not.
                  rowKey={(row) => String((row as { id?: string }).id ?? JSON.stringify(row))}
                  minWidth={520}
                />
              ) : (
                <SutrEmpty title="No access rules" />
              )
            ) : null}
          </>
        ) : null}

        {capabilities.data ? (
          <>
            <SutrSectionLabel>What this install enforces</SutrSectionLabel>
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
