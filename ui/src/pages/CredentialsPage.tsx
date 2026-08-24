import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { KeyRound, Plug } from 'lucide-react'
import { useCatalogStore } from '@/stores/catalog'
import { ConnectedAccountsPanel } from '@/components/connections/ConnectedAccountsPanel'
import { relativeTime } from '@/lib/format'
import {
  SutrBadge,
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrEmpty,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrStatus,
  SutrTable,
  type Column,
} from '@/components/sutr'
import type { InstalledIntegration } from '@/api/client'

/**
 * Where credentials live, and what they authorize — never the values.
 *
 * Two kinds: connected accounts (OAuth grants Sutr holds on your behalf, used
 * to read specifications and to deploy) and integration credentials (the
 * upstream tokens injected into tool calls server-side).
 */
export default function CredentialsPage() {
  const navigate = useNavigate()
  const { installed, integrations, loaded, load } = useCatalogStore()

  useEffect(() => {
    void load()
  }, [load])

  const columns: Column<InstalledIntegration>[] = [
    {
      key: 'integration',
      header: 'Integration',
      render: (row) => {
        const definition = integrations.find((i) => i.id === row.integration_id)
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
            <span className="sutr-table__primary">{definition?.name ?? row.integration_id}</span>
            <span className="sutr-meta sutr-mono">{row.integration_id}</span>
          </div>
        )
      },
    },
    {
      key: 'auth',
      header: 'Credential type',
      width: 150,
      render: (row) => (
        <SutrBadge tone="neutral" plain>
          {row.auth_method}
        </SutrBadge>
      ),
    },
    {
      key: 'where',
      header: 'Injected',
      width: 180,
      render: (row) => (
        <span className="sutr-meta">
          {row.auth_method === 'oauth'
            ? 'Authorization header, server-side'
            : row.auth_method === 'env_var'
              ? 'Environment variable on this host'
              : 'Request header, server-side'}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      width: 150,
      render: (row) => (
        <SutrStatus domain="connection" value={row.connected ? 'connected' : 'disconnected'} />
      ),
    },
    {
      key: 'added',
      header: 'Added',
      width: 130,
      render: (row) => <span className="sutr-meta">{relativeTime(row.added_at)}</span>,
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Govern"
        title="Credentials"
        subtitle="Every grant this instance holds, what it authorizes, and where it is injected. Secret values are never displayed or returned by the API."
      />

      <SutrPageBody>
        <SutrCard>
          <SutrCardHeader
            title="Connected accounts"
            meta="Personal OAuth grants used to read specifications and to deploy into your own cloud accounts"
          />
          <SutrCardBody>
            <ConnectedAccountsPanel />
          </SutrCardBody>
        </SutrCard>

        <SutrCard>
          <SutrCardHeader
            title="Integration credentials"
            meta="Upstream tokens held for installed integrations"
            actions={
              <SutrButton variant="ghost" size="sm" onClick={() => navigate('/app/integrations')}>
                Manage integrations
              </SutrButton>
            }
          />
          <SutrCardBody>
            {!loaded ? (
              <span className="sutr-skeleton" style={{ height: 48 }} />
            ) : (
              <SutrTable
                minWidth={620}
                columns={columns}
                rows={installed}
                rowKey={(row) => row.integration_id}
                onRowClick={(row) =>
                  navigate(`/app/integrations/${encodeURIComponent(row.integration_id)}`)
                }
                empty={
                  <SutrEmpty
                    icon={<Plug size={17} />}
                    title="No integration credentials stored"
                    body="Connect an integration and its credential is stored through the configured secrets backend, then injected into each tool call server-side."
                    action={
                      <SutrButton
                        variant="brand"
                        size="sm"
                        onClick={() => navigate('/app/integrations')}
                      >
                        Connect an integration
                      </SutrButton>
                    }
                  />
                }
              />
            )}
          </SutrCardBody>
        </SutrCard>

        <SutrCard>
          <SutrCardHeader
            title="Programmatic access"
            meta="Keys your own services and agents use to reach this instance"
            actions={
              <SutrButton variant="ghost" size="sm" onClick={() => navigate('/app/api-keys')}>
                <KeyRound size={13} /> API keys
              </SutrButton>
            }
          />
          <SutrCardBody>
            <p className="sutr-body">
              An API key identifies a caller to Sutr; it never grants access to an upstream provider
              directly. Every call it makes is still checked against the tool’s policy and written
              to the audit log.
            </p>
          </SutrCardBody>
        </SutrCard>
      </SutrPageBody>
    </SutrPage>
  )
}
