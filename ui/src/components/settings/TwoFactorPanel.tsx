import { useEffect, useState } from 'react'
import { Key, ShieldCheck } from 'lucide-react'
import { api, type TotpStatusResponse } from '@/api/client'
import { TotpCodeDialog } from '@/components/totp/TotpCodeDialog'
import { TotpSetupDialog } from '@/components/totp/TotpSetupDialog'
import { SutrError, describeError } from '@/components/sutr'

/**
 * Two-factor authentication for this account.
 *
 * Extracted from the settings page so the security surface is one component
 * rather than a block inside a long form. Behaviour is unchanged: enabling
 * runs the setup dialog, and every later change is itself gated by a code.
 */
export function TwoFactorPanel() {
  const [status, setStatus] = useState<TotpStatusResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [setupOpen, setSetupOpen] = useState(false)
  const [toggleAction, setToggleAction] = useState<'disable' | 're-enable' | null>(null)

  useEffect(() => {
    api.totp
      .status()
      .then(setStatus)
      .catch((e) => {
        setStatus({ enabled: false, configured: false })
        setError(describeError(e).message)
      })
      .finally(() => setLoading(false))
  }, [])

  function onToggle() {
    if (!status) return
    setError('')
    if (status.enabled) {
      setToggleAction('disable')
      return
    }
    if (status.configured) {
      setToggleAction('re-enable')
      return
    }
    setSetupOpen(true)
  }

  async function onConfirmToggle(code: string) {
    if (!status || !toggleAction) return
    if (toggleAction === 'disable') {
      await api.totp.disable(code)
      setStatus({ enabled: false, configured: status.configured })
      return
    }
    setStatus(await api.totp.reEnable(code))
  }

  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
      <span
        style={{
          width: 30,
          height: 30,
          borderRadius: 'var(--r-sm)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'var(--brand-soft)',
          color: 'var(--brand)',
          flexShrink: 0,
        }}
      >
        <ShieldCheck size={15} />
      </span>

      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 550, color: 'var(--text)' }}>
              One-time codes on sensitive actions
            </div>
            <div className="sutr-meta" style={{ marginTop: 2 }}>
              Approving a request or changing a tool policy asks for a 6-digit code from your
              authenticator app.
            </div>
          </div>
          <Toggle checked={Boolean(status?.enabled)} disabled={loading} onChange={onToggle} />
        </div>

        {!loading && status?.enabled ? (
          <span className="sutr-meta" style={{ color: 'var(--green)' }}>
            Active — approvals require a code.
          </span>
        ) : null}

        {!loading && !status?.enabled && status?.configured ? (
          <span
            className="sutr-meta"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
          >
            <Key size={12} /> Authenticator is set up — toggle on to resume requiring codes.
          </span>
        ) : null}

        {error ? <SutrError what="Two-factor settings could not be read." why={error} /> : null}
      </div>

      <TotpSetupDialog
        open={setupOpen}
        onClose={() => setSetupOpen(false)}
        onEnabled={() => setStatus({ enabled: true, configured: true })}
      />
      <TotpCodeDialog
        open={toggleAction !== null}
        title={
          toggleAction === 'disable'
            ? 'Disable two-factor authentication'
            : 'Re-enable two-factor authentication'
        }
        description={
          toggleAction === 'disable'
            ? 'Enter a current authenticator code or recovery code before approvals stop requiring one-time codes.'
            : 'Enter a current authenticator code or recovery code before approvals start requiring one-time codes again.'
        }
        confirmLabel={toggleAction === 'disable' ? 'Disable' : 'Enable'}
        onClose={() => setToggleAction(null)}
        onSubmit={onConfirmToggle}
      />
    </div>
  )
}

function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean
  onChange: () => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label="Two-factor authentication"
      onClick={onChange}
      disabled={disabled}
      style={{
        position: 'relative',
        width: 34,
        height: 20,
        borderRadius: 999,
        border: '1px solid var(--border-strong)',
        background: checked ? 'var(--brand)' : 'var(--surface)',
        cursor: disabled ? 'default' : 'pointer',
        transition: 'background var(--dur-fast) var(--ease)',
        flexShrink: 0,
        opacity: disabled ? 0.6 : 1,
        padding: 0,
      }}
    >
      <span
        style={{
          position: 'absolute',
          top: 1,
          left: checked ? 15 : 1,
          width: 16,
          height: 16,
          borderRadius: '50%',
          background: checked ? 'var(--brand-contrast)' : 'var(--text-faint)',
          transition: 'left var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease)',
        }}
      />
    </button>
  )
}
