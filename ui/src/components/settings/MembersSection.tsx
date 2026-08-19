import { useCallback, useEffect, useState } from 'react'
import { Users } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { api, type OrgInfo, type OrgInvitation, type OrgMember } from '@/api/client'

const ROLES = ['owner', 'admin', 'developer', 'member', 'viewer'] as const

const ROLE_DESCRIPTIONS: Record<string, string> = {
  owner: 'Full control, including billing and ownership changes',
  admin: 'Manage members, policies, and approvals',
  developer: 'Manage integrations and API keys, run tools',
  member: 'Run tools',
  viewer: 'Read-only access to logs and tools',
}

/**
 * Organization panel for the settings page: rename, member roles, invitations.
 * Management controls appear only for roles that hold the permission — the
 * server enforces this regardless.
 */
export function MembersSection() {
  const [org, setOrg] = useState<OrgInfo | null>(null)
  const [members, setMembers] = useState<OrgMember[]>([])
  const [invitations, setInvitations] = useState<OrgInvitation[]>([])
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState('member')
  const [inviteUrl, setInviteUrl] = useState('')
  const [orgName, setOrgName] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const canManageMembers = org?.role === 'owner' || org?.role === 'admin'
  const isOwner = org?.role === 'owner'

  const reload = useCallback(async () => {
    try {
      const info = await api.org.get()
      setOrg(info)
      setOrgName(info.name)
      setMembers(await api.org.listMembers())
      if (info.role === 'owner' || info.role === 'admin') {
        const invs = await api.org.listInvitations()
        setInvitations(invs.filter((i) => i.status === 'pending'))
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load organization')
    }
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  async function run(action: () => Promise<void>) {
    setError('')
    setNotice('')
    setBusy(true)
    try {
      await action()
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Action failed')
    } finally {
      setBusy(false)
    }
  }

  async function invite(e: { preventDefault(): void }) {
    e.preventDefault()
    setInviteUrl('')
    await run(async () => {
      const res = await api.org.createInvitation(inviteEmail.trim(), inviteRole)
      setInviteEmail('')
      setInviteUrl(res.invite_url)
      setNotice(`Invitation created for ${res.email}.`)
    })
  }

  return (
    <div style={panelStyle}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
        <div style={iconStyle}>
          <Users size={14} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={titleStyle}>Organization</div>
          <div style={subtitleStyle}>Name, members, and roles.</div>

          {isOwner && (
            <form
              onSubmit={(e) => {
                e.preventDefault()
                run(async () => {
                  await api.org.rename(orgName.trim())
                  setNotice('Organization renamed.')
                })
              }}
              style={{ display: 'flex', gap: 8, marginTop: 12 }}
            >
              <Input
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                maxLength={120}
                style={{ ...inputStyle, maxWidth: 260 }}
                disabled={busy}
              />
              <Button type="submit" size="sm" disabled={busy || orgName.trim() === org?.name}>
                Rename
              </Button>
            </form>
          )}

          <div style={{ marginTop: 16 }}>
            {members.map((m) => (
              <div key={m.user_id} style={memberRowStyle}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div
                    style={{
                      fontSize: 13,
                      color: 'var(--text)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {m.email}
                    {m.is_you && <span style={{ color: 'var(--text-faint)' }}> (you)</span>}
                  </div>
                </div>
                {canManageMembers && !m.is_you ? (
                  <>
                    <select
                      value={m.role}
                      disabled={busy || (m.role === 'owner' && !isOwner)}
                      onChange={(e) =>
                        run(() =>
                          api.org.updateMemberRole(m.user_id, e.target.value).then(() => {}),
                        )
                      }
                      style={selectStyle}
                      title={ROLE_DESCRIPTIONS[m.role]}
                    >
                      {ROLES.map((r) => (
                        <option key={r} value={r} disabled={r === 'owner' && !isOwner}>
                          {r}
                        </option>
                      ))}
                    </select>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy || (m.role === 'owner' && !isOwner)}
                      onClick={() => run(() => api.org.removeMember(m.user_id))}
                      style={{ color: 'var(--red)' }}
                    >
                      Remove
                    </Button>
                  </>
                ) : (
                  <span style={roleBadgeStyle} title={ROLE_DESCRIPTIONS[m.role]}>
                    {m.role}
                  </span>
                )}
              </div>
            ))}
          </div>

          {canManageMembers && (
            <>
              <form onSubmit={invite} style={{ display: 'flex', gap: 8, marginTop: 14 }}>
                <Input
                  type="email"
                  placeholder="teammate@example.com"
                  value={inviteEmail}
                  onChange={(e) => setInviteEmail(e.target.value)}
                  required
                  style={{ ...inputStyle, flex: 1, minWidth: 0 }}
                  disabled={busy}
                />
                <select
                  value={inviteRole}
                  onChange={(e) => setInviteRole(e.target.value)}
                  style={selectStyle}
                  disabled={busy}
                  title={ROLE_DESCRIPTIONS[inviteRole]}
                >
                  {ROLES.filter((r) => r !== 'owner' || isOwner).map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <Button type="submit" size="sm" disabled={busy || !inviteEmail.trim()}>
                  Invite
                </Button>
              </form>

              {inviteUrl && (
                <div style={inviteUrlBoxStyle}>
                  <div style={{ fontSize: 11, color: 'var(--text-dim)', marginBottom: 4 }}>
                    Share this link with the invitee (also emailed if email is configured):
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <code
                      style={{
                        fontSize: 11,
                        color: 'var(--text)',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        flex: 1,
                      }}
                    >
                      {inviteUrl}
                    </code>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() => navigator.clipboard?.writeText(inviteUrl)}
                    >
                      Copy
                    </Button>
                  </div>
                </div>
              )}

              {invitations.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div style={{ fontSize: 11, color: 'var(--text-faint)', marginBottom: 6 }}>
                    Pending invitations
                  </div>
                  {invitations.map((inv) => (
                    <div key={inv.id} style={memberRowStyle}>
                      <div style={{ flex: 1, fontSize: 13, color: 'var(--text-dim)' }}>
                        {inv.email}
                        <span style={{ color: 'var(--text-faint)' }}> — {inv.role}</span>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={busy}
                        onClick={() => run(() => api.org.revokeInvitation(inv.id))}
                        style={{ color: 'var(--red)' }}
                      >
                        Revoke
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {error && (
            <p style={{ fontSize: 12, color: 'var(--red)', margin: '10px 0 0' }}>{error}</p>
          )}
          {notice && (
            <p style={{ fontSize: 12, color: 'var(--green, #22c55e)', margin: '10px 0 0' }}>
              {notice}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}

const panelStyle: React.CSSProperties = {
  border: '1px solid var(--border)',
  borderRadius: 10,
  padding: 16,
  marginBottom: 32,
  background: 'var(--content-bg)',
}

const iconStyle: React.CSSProperties = {
  width: 28,
  height: 28,
  borderRadius: 8,
  border: '1px solid var(--border)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  color: 'var(--text-dim)',
  flexShrink: 0,
}

const titleStyle: React.CSSProperties = {
  fontSize: 13,
  fontWeight: 600,
  color: 'var(--text)',
}

const subtitleStyle: React.CSSProperties = {
  fontSize: 12,
  color: 'var(--text-dim)',
  marginTop: 2,
}

const memberRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '8px 0',
  borderBottom: '1px solid var(--border)',
}

const inputStyle: React.CSSProperties = {
  background: 'var(--input-bg)',
  border: '1px solid var(--border)',
  color: 'var(--text)',
  fontSize: 13,
}

const selectStyle: React.CSSProperties = {
  background: 'var(--input-bg)',
  border: '1px solid var(--border)',
  color: 'var(--text)',
  fontSize: 12,
  borderRadius: 6,
  padding: '4px 8px',
}

const roleBadgeStyle: React.CSSProperties = {
  fontSize: 11,
  color: 'var(--text-dim)',
  border: '1px solid var(--border)',
  borderRadius: 999,
  padding: '2px 10px',
  textTransform: 'capitalize',
}

const inviteUrlBoxStyle: React.CSSProperties = {
  marginTop: 10,
  padding: '8px 10px',
  border: '1px dashed var(--border)',
  borderRadius: 8,
  background: 'var(--bg)',
}
