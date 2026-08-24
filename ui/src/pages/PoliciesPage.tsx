import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ShieldAlert, ShieldCheck, ShieldX } from 'lucide-react'
import { api, type OrgSettingsResponse, type Tool } from '@/api/client'
import { useCatalogStore } from '@/stores/catalog'
import { isTotpChallengeError } from '@/lib/totpError'
import { TotpCodeDialog } from '@/components/totp/TotpCodeDialog'
import {
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrEmpty,
  SutrError,
  SutrField,
  SutrInput,
  SutrMetric,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSegmented,
  SutrTable,
  describeError,
  type Column,
} from '@/components/sutr'

const MODES = [
  { value: 'allow', label: 'Auto' },
  { value: 'require_approval', label: 'Ask' },
  { value: 'deny', label: 'Deny' },
]

/**
 * Governance in one place: how long an approval stays open, and which tools
 * depart from the default of executing automatically.
 *
 * Both settings are enforced server-side; this page only shows and changes
 * them. Nothing is stored client-side, and a TOTP-protected organisation is
 * challenged before a policy moves.
 */
export default function PoliciesPage() {
  const navigate = useNavigate()
  const { tools, loaded, load, refresh } = useCatalogStore()

  const [settings, setSettings] = useState<OrgSettingsResponse | null>(null)
  const [expiry, setExpiry] = useState('')
  const [settingsError, setSettingsError] = useState<string | null>(null)
  const [savingSettings, setSavingSettings] = useState(false)
  const [savedNotice, setSavedNotice] = useState(false)

  const [policyError, setPolicyError] = useState<string | null>(null)
  const [totpOpen, setTotpOpen] = useState(false)
  const [pending, setPending] = useState<{ tool: Tool; mode: string } | null>(null)

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    api.orgSettings
      .get()
      .then((value) => {
        setSettings(value)
        setExpiry(String(value.approval_expiry_minutes))
      })
      .catch((err) => setSettingsError(describeError(err).message))
  }, [])

  const counts = useMemo(() => {
    const result = { allow: 0, require_approval: 0, deny: 0 }
    for (const tool of tools) {
      const mode = (tool.execution_mode ?? 'allow') as keyof typeof result
      if (mode in result) result[mode] += 1
    }
    return result
  }, [tools])

  const governed = useMemo(
    () => tools.filter((tool) => (tool.execution_mode ?? 'allow') !== 'allow'),
    [tools],
  )

  async function saveExpiry() {
    const minutes = Number(expiry)
    if (!Number.isFinite(minutes) || minutes <= 0) {
      setSettingsError('Approval expiry must be a positive number of minutes.')
      return
    }
    setSavingSettings(true)
    setSettingsError(null)
    try {
      const updated = await api.orgSettings.update(minutes)
      setSettings(updated)
      setExpiry(String(updated.approval_expiry_minutes))
      setSavedNotice(true)
      setTimeout(() => setSavedNotice(false), 2400)
    } catch (err) {
      setSettingsError(describeError(err).message)
    } finally {
      setSavingSettings(false)
    }
  }

  async function applyMode(tool: Tool, mode: string, totpCode?: string) {
    if (!tool.integration_id) return
    setPolicyError(null)
    try {
      await api.toolSettings.update(tool.integration_id, tool.name, mode, totpCode)
      await refresh()
      setPending(null)
    } catch (err) {
      if (isTotpChallengeError(err)) {
        setPending({ tool, mode })
        setTotpOpen(true)
        return
      }
      setPolicyError(describeError(err).message)
    }
  }

  const columns: Column<Tool>[] = [
    {
      key: 'tool',
      header: 'Tool',
      render: (tool) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          <code className="sutr-mono sutr-table__primary">{tool.name}</code>
          <span className="sutr-meta sutr-mono">{tool.integration_id}</span>
        </div>
      ),
    },
    {
      key: 'policy',
      header: 'Policy',
      width: 230,
      render: (tool) => (
        <SutrSegmented
          ariaLabel={`Policy for ${tool.name}`}
          value={tool.execution_mode ?? 'allow'}
          onChange={(mode) => void applyMode(tool, mode)}
          items={MODES}
        />
      ),
    },
  ]

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Govern"
        title="Policies"
        subtitle="What executes on its own, what waits for a person, and what never runs at all."
        actions={
          <SutrButton variant="secondary" size="sm" onClick={() => navigate('/app/tools')}>
            Open tool catalog
          </SutrButton>
        }
      />

      <SutrPageBody>
        <div className="sutr-grid sutr-grid--metrics">
          <SutrMetric
            label="Auto approve"
            value={loaded ? counts.allow : null}
            icon={<ShieldCheck size={12} />}
            foot="Execute immediately, still audited"
          />
          <SutrMetric
            label="Ask first"
            value={loaded ? counts.require_approval : null}
            icon={<ShieldAlert size={12} />}
            foot="Held until a person decides"
            muted={loaded && counts.require_approval === 0}
          />
          <SutrMetric
            label="Denied"
            value={loaded ? counts.deny : null}
            icon={<ShieldX size={12} />}
            foot="Refused before reaching the provider"
            muted={loaded && counts.deny === 0}
          />
          <SutrMetric
            label="Approval window"
            value={settings ? `${settings.approval_expiry_minutes} min` : null}
            foot={
              settings?.approval_expiry_minutes_override === null
                ? `instance default (${settings?.approval_expiry_minutes_default} min)`
                : 'set for this organisation'
            }
          />
        </div>

        <SutrCard>
          <SutrCardHeader
            title="Approval window"
            meta="How long a pending request stays open before it expires unanswered"
          />
          <SutrCardBody>
            <div style={{ display: 'flex', gap: 12, alignItems: 'flex-end', flexWrap: 'wrap' }}>
              <div style={{ width: 220 }}>
                <SutrField
                  label="Minutes"
                  hint="An expired request cannot be approved; the agent must ask again."
                >
                  {(props) => (
                    <SutrInput
                      {...props}
                      type="number"
                      min={1}
                      value={expiry}
                      onChange={(e) => setExpiry(e.target.value)}
                    />
                  )}
                </SutrField>
              </div>
              <SutrButton
                variant="primary"
                onClick={() => void saveExpiry()}
                loading={savingSettings}
              >
                Save
              </SutrButton>
              {savedNotice ? (
                <span className="sutr-meta" style={{ color: 'var(--green)' }}>
                  Saved
                </span>
              ) : null}
            </div>
            {settingsError ? (
              <div style={{ marginTop: 12 }}>
                <SutrError what="The approval window was not changed." why={settingsError} />
              </div>
            ) : null}
          </SutrCardBody>
        </SutrCard>

        <SutrCard>
          <SutrCardHeader
            title="Tools with a non-default policy"
            meta="Every other tool executes automatically under the organisation default"
          />
          <SutrCardBody>
            {policyError ? (
              <div style={{ marginBottom: 12 }}>
                <SutrError what="The policy was not changed." why={policyError} />
              </div>
            ) : null}
            {!loaded ? (
              <span className="sutr-skeleton" style={{ height: 48 }} />
            ) : (
              <SutrTable
                minWidth={520}
                columns={columns}
                rows={governed}
                rowKey={(tool) => `${tool.integration_id}:${tool.name}`}
                empty={
                  <SutrEmpty
                    icon={<ShieldCheck size={17} />}
                    title="Every tool executes automatically"
                    body="Nothing is currently held for approval or denied. Open the tool catalog to tighten a specific capability — anything that writes, refunds or deletes is a good candidate."
                    action={
                      <SutrButton
                        variant="secondary"
                        size="sm"
                        onClick={() => navigate('/app/tools')}
                      >
                        Review tools
                      </SutrButton>
                    }
                  />
                }
              />
            )}
          </SutrCardBody>
        </SutrCard>
      </SutrPageBody>

      <TotpCodeDialog
        open={totpOpen}
        title="Confirm policy change"
        description="Enter your authenticator code to change how this tool executes."
        confirmLabel="Confirm"
        onClose={() => {
          setTotpOpen(false)
          setPending(null)
        }}
        onSubmit={async (code) => {
          if (pending) await applyMode(pending.tool, pending.mode, code)
        }}
      />
    </SutrPage>
  )
}
