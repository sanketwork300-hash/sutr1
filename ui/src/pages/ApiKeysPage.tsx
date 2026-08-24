import { useEffect, useState } from 'react'
import { KeyRound, Plus, Trash2 } from 'lucide-react'
import { api, type ApiKey, type CreateApiKeyResponse } from '@/api/client'
import { formatDateTime, relativeTime } from '@/lib/format'
import {
  SutrBadge,
  SutrButton,
  SutrCodeBlock,
  SutrEmpty,
  SutrError,
  SutrField,
  SutrInput,
  SutrModal,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrTable,
  describeError,
  type Column,
} from '@/components/sutr'

/**
 * API keys for programmatic access. The plaintext key is returned exactly once,
 * at creation; after that only its prefix exists, here and on the server.
 */
export default function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKey[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [created, setCreated] = useState<CreateApiKeyResponse | null>(null)

  async function reload() {
    try {
      setKeys(await api.apiKeys.list())
      setError(null)
    } catch (err) {
      setError(describeError(err).message)
      setKeys([])
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  async function create() {
    if (!name.trim()) return
    setBusy(true)
    setError(null)
    try {
      const key = await api.apiKeys.create(name.trim())
      setCreated(key)
      setCreateOpen(false)
      setName('')
      await reload()
    } catch (err) {
      setError(describeError(err).message)
    } finally {
      setBusy(false)
    }
  }

  async function revoke(key: ApiKey) {
    if (
      !window.confirm(
        `Revoke “${key.name}”? Any agent or service still presenting it stops working immediately.`,
      )
    ) {
      return
    }
    try {
      await api.apiKeys.revoke(key.id)
      await reload()
    } catch (err) {
      setError(describeError(err).message)
    }
  }

  const columns: Column<ApiKey>[] = [
    {
      key: 'name',
      header: 'Key',
      render: (key) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          <span className="sutr-table__primary">{key.name}</span>
          <span className="sutr-meta sutr-mono">{key.key_prefix}…</span>
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      width: 120,
      render: (key) =>
        key.is_active ? (
          <SutrBadge tone="success" dot>
            ACTIVE
          </SutrBadge>
        ) : (
          <SutrBadge tone="neutral" dot>
            REVOKED
          </SutrBadge>
        ),
    },
    {
      key: 'last_used',
      header: 'Last used',
      width: 160,
      render: (key) =>
        key.last_used_at ? (
          <span title={formatDateTime(key.last_used_at)}>{relativeTime(key.last_used_at)}</span>
        ) : (
          <span className="sutr-meta">never</span>
        ),
    },
    {
      key: 'created',
      header: 'Created',
      width: 160,
      render: (key) => <span className="sutr-meta">{formatDateTime(key.created_at)}</span>,
    },
    {
      key: 'actions',
      header: '',
      width: 90,
      render: (key) =>
        key.is_active ? (
          <div className="sutr-table__actions">
            <SutrButton variant="ghost" size="sm" onClick={() => void revoke(key)}>
              <Trash2 size={13} /> Revoke
            </SutrButton>
          </div>
        ) : null,
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Develop"
        title="API keys"
        subtitle="Credentials your services and agents present to this instance. Every call they make is governed by the same policies as the console."
        actions={
          <SutrButton variant="brand" size="sm" onClick={() => setCreateOpen(true)}>
            <Plus size={13} /> Create key
          </SutrButton>
        }
      />

      <SutrPageBody>
        {error ? <SutrError what="That action did not complete." why={error} /> : null}

        {keys === null ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {[0, 1].map((i) => (
              <span key={i} className="sutr-skeleton" style={{ height: 44 }} />
            ))}
          </div>
        ) : (
          <SutrTable
            columns={columns}
            rows={keys}
            rowKey={(key) => key.id}
            minWidth={720}
            empty={
              <SutrEmpty
                icon={<KeyRound size={17} />}
                title="No API keys yet"
                body="Create a key to call tools from your own code, the SDKs or the CLI. The key is shown once and stored hashed."
                action={
                  <SutrButton variant="brand" size="sm" onClick={() => setCreateOpen(true)}>
                    Create the first key
                  </SutrButton>
                }
              />
            }
          />
        )}
      </SutrPageBody>

      <SutrModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="Create an API key"
        description="Name it after the caller it belongs to, so revoking it later is an obvious decision."
        footer={
          <>
            <SutrButton variant="ghost" onClick={() => setCreateOpen(false)}>
              Cancel
            </SutrButton>
            <SutrButton variant="brand" loading={busy} onClick={() => void create()}>
              Create key
            </SutrButton>
          </>
        }
      >
        <SutrField label="Name" hint="For example: support-agent, ci-pipeline, laptop-cli.">
          {(props) => (
            <SutrInput
              {...props}
              value={name}
              autoFocus
              placeholder="support-agent"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void create()
              }}
            />
          )}
        </SutrField>
      </SutrModal>

      <SutrModal
        open={Boolean(created)}
        onClose={() => setCreated(null)}
        title="Copy this key now"
        description="This is the only time the full key is shown. Sutr stores only its hash."
        footer={
          <SutrButton variant="primary" onClick={() => setCreated(null)}>
            Done
          </SutrButton>
        }
      >
        {created ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <SutrCodeBlock code={created.plain_key} label={created.name} />
            <p className="sutr-meta">
              Set it as <code className="sutr-code--inline">SUTR_API_KEY</code> for the SDKs, or
              pass it to <code className="sutr-code--inline">sutr auth login --api-key</code>.
            </p>
          </div>
        ) : null}
      </SutrModal>
    </SutrPage>
  )
}
