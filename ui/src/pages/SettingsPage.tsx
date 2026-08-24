import { useState, type SubmitEvent } from 'react'
import { Link } from 'react-router-dom'
import { KeyRound, Moon, ShieldCheck, Sun, Users } from 'lucide-react'
import { api } from '@/api/client'
import { TwoFactorPanel } from '@/components/settings/TwoFactorPanel'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import { useConfigStore } from '@/stores/config'
import {
  SutrButton,
  SutrCard,
  SutrCardBody,
  SutrCardHeader,
  SutrError,
  SutrField,
  SutrInput,
  SutrPage,
  SutrPageBody,
  SutrPageHeader,
  SutrSegmented,
  SutrTabs,
  describeError,
} from '@/components/sutr'

type Section = 'profile' | 'security' | 'appearance'

/**
 * Account settings only.
 *
 * Organisation membership, workspaces, credentials and approval policy each
 * have a governance page of their own now — this page links to them instead of
 * duplicating the panels, so there is exactly one place to change each thing.
 */
export default function SettingsPage() {
  const [section, setSection] = useState<Section>('profile')
  const email = useAuthStore((s) => s.email)
  const { theme, toggle } = useThemeStore()
  const isSelfHosted = useConfigStore((s) => s.isSelfHosted)

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [loading, setLoading] = useState(false)

  async function onChangePassword(e: SubmitEvent<HTMLFormElement>) {
    e.preventDefault()
    setError('')
    setSuccess('')

    if (newPassword !== confirmPassword) {
      setError('The new passwords do not match.')
      return
    }
    if (newPassword.length < 6) {
      setError('The new password must be at least 6 characters.')
      return
    }

    setLoading(true)
    try {
      const res = await api.auth.changePassword(currentPassword, newPassword)
      setSuccess(res.message)
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
    } catch (err) {
      setError(describeError(err).message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <SutrPage>
      <SutrPageHeader
        eyebrow="Account"
        title="Settings"
        subtitle="Your account, your credentials, and how this console looks."
      >
        <SutrTabs
          ariaLabel="Settings section"
          value={section}
          onChange={setSection}
          items={[
            { value: 'profile', label: 'Profile' },
            { value: 'security', label: 'Security' },
            { value: 'appearance', label: 'Appearance' },
          ]}
        />
      </SutrPageHeader>

      <SutrPageBody>
        {section === 'profile' ? (
          <>
            <SutrCard>
              <SutrCardHeader title="Account" meta="Who you are signed in as" />
              <SutrCardBody>
                <dl className="sutr-dl">
                  <dt className="sutr-dl__key">Email</dt>
                  <dd className="sutr-dl__val" style={{ margin: 0 }}>
                    {email ?? '—'}
                  </dd>
                </dl>
              </SutrCardBody>
            </SutrCard>

            <SutrCard>
              <SutrCardHeader
                title="Where the rest lives"
                meta="Each of these is governed on its own page"
              />
              <SutrCardBody
                style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}
              >
                <Link to="/app/access" className="sutr-btn sutr-btn--secondary sutr-btn--sm">
                  <Users size={13} /> Members and workspaces
                </Link>
                <Link to="/app/policies" className="sutr-btn sutr-btn--secondary sutr-btn--sm">
                  <ShieldCheck size={13} /> Approval policy
                </Link>
                <Link to="/app/credentials" className="sutr-btn sutr-btn--secondary sutr-btn--sm">
                  <KeyRound size={13} /> Connected accounts
                </Link>
                <Link to="/app/api-keys" className="sutr-btn sutr-btn--secondary sutr-btn--sm">
                  <KeyRound size={13} /> API keys
                </Link>
                {!isSelfHosted ? (
                  <Link
                    to="/app/settings/billing"
                    className="sutr-btn sutr-btn--secondary sutr-btn--sm"
                  >
                    Billing
                  </Link>
                ) : null}
              </SutrCardBody>
            </SutrCard>
          </>
        ) : null}

        {section === 'security' ? (
          <>
            <SutrCard>
              <SutrCardHeader
                title="Two-factor authentication"
                meta="Applies to approvals and policy changes"
              />
              <SutrCardBody>
                <TwoFactorPanel />
              </SutrCardBody>
            </SutrCard>

            <SutrCard>
              <SutrCardHeader title="Change password" meta="At least 6 characters" />
              <SutrCardBody>
                <form
                  onSubmit={onChangePassword}
                  style={{ display: 'flex', flexDirection: 'column', gap: 12, maxWidth: 380 }}
                >
                  <SutrField label="Current password">
                    {(props) => (
                      <SutrInput
                        {...props}
                        type="password"
                        autoComplete="current-password"
                        value={currentPassword}
                        onChange={(e) => setCurrentPassword(e.target.value)}
                        required
                      />
                    )}
                  </SutrField>
                  <SutrField label="New password">
                    {(props) => (
                      <SutrInput
                        {...props}
                        type="password"
                        autoComplete="new-password"
                        value={newPassword}
                        onChange={(e) => setNewPassword(e.target.value)}
                        required
                        minLength={6}
                      />
                    )}
                  </SutrField>
                  <SutrField label="Confirm new password">
                    {(props) => (
                      <SutrInput
                        {...props}
                        type="password"
                        autoComplete="new-password"
                        value={confirmPassword}
                        onChange={(e) => setConfirmPassword(e.target.value)}
                        required
                        minLength={6}
                      />
                    )}
                  </SutrField>

                  {error ? <SutrError what="The password was not changed." why={error} /> : null}
                  {success ? (
                    <span className="sutr-meta" style={{ color: 'var(--green)' }}>
                      {success}
                    </span>
                  ) : null}

                  <div>
                    <SutrButton type="submit" variant="primary" loading={loading}>
                      Change password
                    </SutrButton>
                  </div>
                </form>
              </SutrCardBody>
            </SutrCard>
          </>
        ) : null}

        {section === 'appearance' ? (
          <SutrCard>
            <SutrCardHeader
              title="Appearance"
              meta="Sutr is designed dark-first; the light theme is a full alternative, not a fallback"
            />
            <SutrCardBody>
              <SutrSegmented
                ariaLabel="Theme"
                value={theme}
                onChange={(next) => {
                  if (next !== theme) toggle()
                }}
                items={[
                  {
                    value: 'dark',
                    label: (
                      <>
                        <Moon size={12} /> Dark
                      </>
                    ),
                  },
                  {
                    value: 'light',
                    label: (
                      <>
                        <Sun size={12} /> Light
                      </>
                    ),
                  },
                ]}
              />
              <p className="sutr-meta" style={{ marginTop: 10 }}>
                Saved in this browser. Reduced-motion preferences from your system are respected
                everywhere in the console.
              </p>
            </SutrCardBody>
          </SutrCard>
        ) : null}
      </SutrPageBody>
    </SutrPage>
  )
}
