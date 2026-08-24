import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Check,
  Cloud,
  ExternalLink,
  Loader2,
  Plug,
  Rocket,
  Server,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  api,
  type AuthorizeResult,
  type ConnectionProviderInfo,
  type Deployment,
  type DeploymentProviderInfo,
  type DeployTarget,
  type OpenApiCompileRequest,
  type OpenApiProject,
} from '@/api/client'
import { AwsDeviceDialog } from '@/components/connections/AwsDeviceDialog'
import {
  useConnectionMessages,
  useProviderConnections,
  startRedirectConnect,
} from '@/components/connections/useProviderConnections'
import {
  Card,
  ChoiceButton,
  Field,
  Footer,
  SecretInput,
} from '@/components/mcp-builder/primitives'
import {
  bannerStyle,
  inputStyle,
  monoInputStyle,
  spin,
} from '@/components/mcp-builder/styles'

const PROVIDER_ICONS: Record<string, React.ReactNode> = {
  docker: <Server size={14} />,
  gcp: <Cloud size={14} />,
  azure: <Cloud size={14} />,
  aws: <Cloud size={14} />,
}

/**
 * The last stage: run the generated server somewhere.
 *
 * The provider list, the fields each provider needs, and the help beside each
 * field all come from the server's provider registry, so this component never
 * hard-codes what Cloud Run or Container Apps or App Runner wants. Adding a
 * provider is a server change.
 *
 * Deploying is deliberately separate from creating the integration: the
 * integration is governed tools inside sutr, this is a standalone server in
 * the user's own cloud. Doing both is common; needing both is not.
 */
export function DeployStep({
  project,
  compileBody,
  suggestedName,
  onBack,
  onOpenDeployments,
}: {
  project: OpenApiProject
  compileBody: (dryRun: boolean) => OpenApiCompileRequest
  suggestedName: string
  onBack: () => void
  onOpenDeployments: () => void
}) {
  const [providers, setProviders] = useState<DeploymentProviderInfo[]>([])
  const [providerId, setProviderId] = useState('')
  // Keyed by provider so switching targets and switching back does not lose
  // what was already typed, and so the declared defaults can be layered
  // underneath at read time instead of copied in by an effect.
  const [edits, setEdits] = useState<Record<string, Record<string, string>>>({})
  const [name, setName] = useState(suggestedName)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [created, setCreated] = useState<Deployment | null>(null)
  const [device, setDevice] = useState<AuthorizeResult | null>(null)
  const [connecting, setConnecting] = useState(false)

  const connections = useProviderConnections()
  const onConnectionChanged = useCallback(() => {
    setConnecting(false)
    void connections.reload()
  }, [connections])
  useConnectionMessages(onConnectionChanged)

  useEffect(() => {
    api.deployments
      .providers()
      .then((list) => {
        setProviders(list)
        const first = list.find((entry) => entry.enabled) ?? list[0]
        if (first) setProviderId(first.id)
      })
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Could not load deployment providers'),
      )
  }, [])

  const provider = useMemo(
    () => providers.find((entry) => entry.id === providerId),
    [providerId, providers],
  )
  const connectionProvider: ConnectionProviderInfo | undefined = provider?.connection_provider
    ? connections.find(provider.connection_provider)
    : undefined
  const connection = connectionProvider?.connection ?? null
  const needsConnection = Boolean(provider?.connection_provider)
  const ready = !needsConnection || (Boolean(connection) && !connection?.expired)

  // Declared defaults under whatever the user has typed, computed rather than
  // copied: the form always shows the values that will actually be sent.
  const config = useMemo(() => {
    const seeded: Record<string, string> = {}
    for (const field of provider?.config_fields ?? []) seeded[field.key] = field.default
    return { ...seeded, ...(edits[providerId] ?? {}) }
  }, [edits, provider?.config_fields, providerId])

  function setConfigValue(key: string, value: string) {
    setEdits((prev) => ({ ...prev, [providerId]: { ...(prev[providerId] ?? {}), [key]: value } }))
  }

  async function connect() {
    if (!provider?.connection_provider) return
    setError('')
    setConnecting(true)
    try {
      if (connectionProvider?.flow === 'device') {
        setDevice(await api.connections.authorize(provider.connection_provider))
        setConnecting(false)
      } else {
        await startRedirectConnect(provider.connection_provider)
      }
    } catch (e) {
      setConnecting(false)
      setError(e instanceof Error ? e.message : 'Could not start the authorization')
    }
  }

  async function deploy() {
    if (!provider) return
    setError('')
    setBusy(true)
    try {
      setCreated(
        await api.deployments.create({
          project_id: project.id,
          name: name.trim() || suggestedName,
          provider: provider.id,
          connection_id: connection?.id ?? null,
          provider_config: config,
          token: token.trim() || null,
          compile: compileBody(false),
        }),
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The deployment could not be created')
    } finally {
      setBusy(false)
    }
  }

  if (created && created.provider === providerId) {
    return (
      <Card
        title="Deployment queued"
        subtitle={`sutr is building and starting "${created.name}" on ${provider?.display_name}. This takes a few minutes on a cloud provider — the image has to be built first.`}
      >
        <div style={{ padding: '18px 24px' }}>
          <div style={{ ...bannerStyle('success'), marginBottom: 14 }}>
            <Check size={14} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              Progress, the MCP URL, and build failures all appear on the Deployments page. You
              can leave this page.
            </span>
          </div>
          <p style={{ margin: 0, fontSize: 12.5, color: 'var(--text-dim)', lineHeight: 1.7 }}>
            The deployed server is standalone: it talks to the API directly and does not route
            through sutr, so sutr&apos;s approval policy does not apply to it. Use the
            integration if you want governed tools.
          </p>
        </div>
        <Footer hint={created.error ? created.error : `Status: ${created.status}`}>
          <Button size="sm" onClick={onOpenDeployments}>
            <Rocket size={13} style={{ marginRight: 5 }} /> Open deployments
          </Button>
        </Footer>
      </Card>
    )
  }

  return (
    <Card
      title="Deploy"
      subtitle="Optional. Run the generated server as a standalone MCP endpoint — on this host with Docker, or in your own cloud account."
    >
      <div style={{ padding: '18px 24px 4px' }}>
        {error && (
          <div style={bannerStyle('error')}>
            <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>{error}</span>
          </div>
        )}

        <Field
          label="Target"
          help="Cloud targets build a container image with that cloud's own build service and run it on its serverless container runtime, scaling to zero when idle."
        >
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: `repeat(${Math.min(providers.length || 1, 4)}, 1fr)`,
              gap: 8,
            }}
          >
            {providers.map((entry) => (
              <ChoiceButton
                key={entry.id}
                active={providerId === entry.id}
                onClick={() => setProviderId(entry.id)}
                icon={PROVIDER_ICONS[entry.id] ?? <Cloud size={14} />}
                label={entry.display_name}
                hint={entry.enabled ? (entry.creates ?? '') : 'Unavailable'}
              />
            ))}
          </div>
          {provider && !provider.enabled && (
            <p style={{ margin: '8px 0 0', fontSize: 11.5, color: 'var(--badge-red-text)' }}>
              {provider.reason}
            </p>
          )}
          {provider?.enabled && provider.creates && (
            <p style={{ margin: '8px 0 0', fontSize: 11.5, color: 'var(--text-dim)', lineHeight: 1.55 }}>
              Creates: {provider.creates}
            </p>
          )}
        </Field>

        {provider?.enabled && needsConnection && (
          <Field
            label={`${connectionProvider?.display_name ?? 'Cloud'} account`}
            help={connectionProvider?.grant_summary}
          >
            {!connectionProvider?.configured ? (
              <p style={{ margin: 0, fontSize: 12, color: 'var(--badge-red-text)' }}>
                {connectionProvider?.reason}
              </p>
            ) : ready ? (
              <div style={connectedRowStyle}>
                <Check size={14} style={{ color: 'var(--badge-green-text)' }} />
                <span style={{ fontSize: 12.5, color: 'var(--text)' }}>
                  Connected as {connection?.account_label || 'your account'}
                </span>
                <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--text-faint)' }}>
                  Manage in Govern → Credentials
                </span>
              </div>
            ) : (
              <div style={connectedRowStyle}>
                <span style={{ fontSize: 12.5, color: 'var(--text)' }}>
                  {connection?.expired
                    ? 'The authorization expired. Reconnect to deploy.'
                    : 'Not connected yet.'}
                </span>
                <Button
                  size="sm"
                  style={{ marginLeft: 'auto' }}
                  onClick={connect}
                  disabled={connecting}
                >
                  {connecting ? (
                    <Loader2 size={13} style={spin} />
                  ) : (
                    <>
                      <Plug size={13} style={{ marginRight: 5 }} /> Connect
                    </>
                  )}
                </Button>
              </div>
            )}
          </Field>
        )}

        {provider?.enabled &&
          ready &&
          (provider.config_fields ?? []).map((field) => (
            <ConfigInput
              key={field.key}
              field={field}
              value={config[field.key] ?? ''}
              config={config}
              connectionId={connection?.id ?? null}
              onChange={(value) => setConfigValue(field.key, value)}
            />
          ))}

        {provider?.enabled && ready && (
          <>
            <Field label="Deployment name" help="Shown on the Deployments page and used to name the resources created in your account.">
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                style={inputStyle}
              />
            </Field>

            <Field
              label="Upstream API token"
              optional
              help="The credential the deployed server sends to the API it wraps. Stored through sutr's secrets backend and injected as an environment variable at run time — it is never written into the package or baked into the image."
            >
              <SecretInput value={token} onChange={setToken} placeholder="Leave empty for an unauthenticated API" />
            </Field>
          </>
        )}
      </div>

      <Footer
        hint={
          ready
            ? 'The build runs in your account and can take several minutes.'
            : 'Connect an account to continue.'
        }
        onBack={onBack}
      >
        <div style={{ display: 'flex', gap: 8 }}>
          <Button size="sm" variant="outline" onClick={onOpenDeployments}>
            All deployments
          </Button>
          <Button
            size="sm"
            onClick={deploy}
            disabled={busy || !provider?.enabled || !ready}
          >
            {busy ? (
              <Loader2 size={13} style={spin} />
            ) : (
              <>
                <Rocket size={13} style={{ marginRight: 5 }} /> Deploy
              </>
            )}
          </Button>
        </div>
      </Footer>

      <AwsDeviceDialog
        authorization={device}
        onConnected={() => {
          setDevice(null)
          void connections.reload()
        }}
        onClose={() => setDevice(null)}
      />
    </Card>
  )
}

/**
 * One provider config field.
 *
 * `target` and `role` fields are pickers fed by the connected account's own
 * inventory: a project id or account number typed from memory is the single
 * most likely thing to be wrong, and the wrongness only shows up minutes into
 * a build.
 */
function ConfigInput({
  field,
  value,
  config,
  connectionId,
  onChange,
}: {
  field: NonNullable<DeploymentProviderInfo['config_fields']>[number]
  value: string
  config: Record<string, string>
  connectionId: string | null
  onChange: (value: string) => void
}) {
  const [targets, setTargets] = useState<DeployTarget[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const isTarget = field.kind === 'target'

  useEffect(() => {
    if (!isTarget || !connectionId) return
    let cancelled = false
    api.connections
      .targets(connectionId)
      .then((result) => {
        if (cancelled) return
        setTargets(result.targets)
        setLoadError('')
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : 'Could not list targets')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [connectionId, isTarget])

  if (isTarget) {
    return (
      <Field label={field.label} help={field.help} optional={!field.required}>
        {loadError ? (
          <>
            <p style={{ margin: '0 0 6px', fontSize: 11.5, color: 'var(--badge-red-text)' }}>
              {loadError} Enter it manually.
            </p>
            <input
              value={value}
              onChange={(event) => onChange(event.target.value)}
              placeholder={field.placeholder}
              style={monoInputStyle}
            />
          </>
        ) : (
          <select
            value={value}
            onChange={(event) => onChange(event.target.value)}
            style={{ ...inputStyle, height: 36 }}
            disabled={loading}
          >
            <option value="">{loading ? 'Loading…' : 'Choose one'}</option>
            {targets.map((target) => (
              <option key={target.id} value={target.id}>
                {target.label}
              </option>
            ))}
          </select>
        )}
      </Field>
    )
  }

  if (field.kind === 'role') {
    // AWS roles are scoped to the chosen account, so they are read from the
    // account picker's own payload rather than fetched again.
    return (
      <RoleField field={field} value={value} config={config} connectionId={connectionId} onChange={onChange} />
    )
  }

  return (
    <Field label={field.label} help={field.help} optional={!field.required}>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={field.placeholder}
        spellCheck={false}
        style={field.kind === 'region' ? monoInputStyle : inputStyle}
      />
    </Field>
  )
}

function RoleField({
  field,
  value,
  config,
  connectionId,
  onChange,
}: {
  field: NonNullable<DeploymentProviderInfo['config_fields']>[number]
  value: string
  config: Record<string, string>
  connectionId: string | null
  onChange: (value: string) => void
}) {
  // Keyed by account rather than replaced: switching accounts must not show
  // the previous account's roles for a frame, and clearing them in an effect
  // is exactly the cascading render that would cause.
  const [rolesByAccount, setRolesByAccount] = useState<Record<string, string[]>>({})
  const account = config.account ?? ''
  const roles = rolesByAccount[account] ?? []

  useEffect(() => {
    if (!connectionId || !account) return
    let cancelled = false
    api.connections
      .targets(connectionId)
      .then((result) => {
        if (cancelled) return
        const match = result.targets.find((target) => target.id === account)
        setRolesByAccount((prev) => ({ ...prev, [account]: match?.roles ?? [] }))
      })
      .catch(() => {
        if (!cancelled) setRolesByAccount((prev) => ({ ...prev, [account]: [] }))
      })
    return () => {
      cancelled = true
    }
  }, [account, connectionId])

  return (
    <Field label={field.label} help={field.help} optional={!field.required}>
      {roles.length > 0 ? (
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          style={{ ...inputStyle, height: 36 }}
        >
          <option value="">Choose one</option>
          {roles.map((role) => (
            <option key={role} value={role}>
              {role}
            </option>
          ))}
        </select>
      ) : (
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={account ? field.placeholder : 'Choose an account first'}
          style={monoInputStyle}
        />
      )}
    </Field>
  )
}

export function DeploymentLink({ deployment }: { deployment: Deployment }) {
  if (!deployment.console_url) return null
  return (
    <a
      href={deployment.console_url}
      target="_blank"
      rel="noreferrer"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontSize: 11.5,
        color: 'var(--text-dim)',
      }}
    >
      <ExternalLink size={11} /> Provider console
    </a>
  )
}

const connectedRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '10px 12px',
  border: '1px solid var(--border)',
  borderRadius: 8,
  background: 'var(--surface)',
}
