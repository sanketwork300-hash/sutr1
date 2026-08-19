import { useEffect, useState } from 'react'
import { Check, Loader2, Pencil, Plus, Trash2, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { api, ApiError, type Workspace } from '@/api/client'

/** Workspaces group an org's integrations and OpenAPI projects. Every org has a
 *  default workspace that cannot be deleted. */
export function WorkspacesPanel() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [editName, setEditName] = useState('')

  async function load() {
    try {
      setWorkspaces(await api.workspaces.list())
      setError('')
    } catch (e) {
      setError(describe(e, 'Failed to load workspaces'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function create() {
    if (!newName.trim()) return
    setBusy('create')
    try {
      await api.workspaces.create(newName.trim())
      setNewName('')
      setCreating(false)
      await load()
    } catch (e) {
      setError(describe(e, 'Failed to create the workspace'))
    } finally {
      setBusy(null)
    }
  }

  async function rename(id: string) {
    if (!editName.trim()) return
    setBusy(id)
    try {
      await api.workspaces.rename(id, editName.trim())
      setEditing(null)
      await load()
    } catch (e) {
      setError(describe(e, 'Failed to rename the workspace'))
    } finally {
      setBusy(null)
    }
  }

  async function remove(workspace: Workspace) {
    if (!window.confirm(`Delete workspace “${workspace.name}”?`)) return
    setBusy(workspace.id)
    try {
      await api.workspaces.remove(workspace.id)
      await load()
    } catch (e) {
      setError(describe(e, 'Failed to delete the workspace'))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div style={{ marginBottom: 32 }}>
      {error && (
        <div
          style={{
            marginBottom: 10,
            padding: '7px 11px',
            borderRadius: 7,
            fontSize: 12,
            background: 'var(--badge-red-bg)',
            color: 'var(--badge-red-text)',
          }}
        >
          {error}
        </div>
      )}

      <div
        style={{
          border: '1px solid var(--border)',
          borderRadius: 9,
          background: 'var(--content-bg)',
          overflow: 'hidden',
        }}
      >
        {loading ? (
          <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-faint)' }}>
            <Loader2 size={15} style={{ animation: 'spin 1s linear infinite' }} />
          </div>
        ) : (
          workspaces.map((workspace, index) => (
            <div
              key={workspace.id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '10px 12px',
                borderTop: index === 0 ? 'none' : '1px solid var(--border)',
              }}
            >
              {editing === workspace.id ? (
                <>
                  <input
                    value={editName}
                    onChange={(event) => setEditName(event.target.value)}
                    onKeyDown={(event) => event.key === 'Enter' && rename(workspace.id)}
                    autoFocus
                    style={inputStyle}
                  />
                  <IconButton title="Save" onClick={() => rename(workspace.id)}>
                    <Check size={13} />
                  </IconButton>
                  <IconButton title="Cancel" onClick={() => setEditing(null)}>
                    <X size={13} />
                  </IconButton>
                </>
              ) : (
                <>
                  <span style={{ fontSize: 13, color: 'var(--text)', flex: 1, minWidth: 0 }}>
                    {workspace.name}
                    {workspace.is_default && (
                      <span
                        style={{
                          marginLeft: 8,
                          fontSize: 10,
                          fontWeight: 600,
                          textTransform: 'uppercase',
                          letterSpacing: 0.4,
                          color: 'var(--text-faint)',
                        }}
                      >
                        default
                      </span>
                    )}
                  </span>
                  {busy === workspace.id ? (
                    <Loader2
                      size={13}
                      style={{ animation: 'spin 1s linear infinite', color: 'var(--text-faint)' }}
                    />
                  ) : (
                    <>
                      <IconButton
                        title="Rename"
                        onClick={() => {
                          setEditing(workspace.id)
                          setEditName(workspace.name)
                        }}
                      >
                        <Pencil size={12} />
                      </IconButton>
                      {!workspace.is_default && (
                        <IconButton title="Delete" danger onClick={() => remove(workspace)}>
                          <Trash2 size={12} />
                        </IconButton>
                      )}
                    </>
                  )}
                </>
              )}
            </div>
          ))
        )}

        {creating ? (
          <div
            style={{
              display: 'flex',
              gap: 8,
              padding: '10px 12px',
              borderTop: '1px solid var(--border)',
            }}
          >
            <input
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && create()}
              placeholder="Workspace name"
              autoFocus
              style={inputStyle}
            />
            <Button size="sm" onClick={create} disabled={busy === 'create'}>
              {busy === 'create' ? (
                <Loader2 size={12} style={{ animation: 'spin 1s linear infinite' }} />
              ) : (
                'Create'
              )}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setCreating(false)}>
              Cancel
            </Button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setCreating(true)}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              width: '100%',
              padding: '10px 12px',
              borderTop: '1px solid var(--border)',
              border: 'none',
              background: 'transparent',
              color: 'var(--text-dim)',
              fontSize: 12.5,
              fontFamily: 'inherit',
              cursor: 'pointer',
            }}
          >
            <Plus size={13} />
            New workspace
          </button>
        )}
      </div>
    </div>
  )
}

function describe(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.status === 403) {
    return 'Only owners and admins can change workspaces.'
  }
  return error instanceof Error ? error.message : fallback
}

function IconButton({
  title,
  onClick,
  danger,
  children,
}: {
  title: string
  onClick: () => void
  danger?: boolean
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      style={{
        width: 26,
        height: 26,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        border: '1px solid var(--border)',
        borderRadius: 6,
        background: 'var(--surface)',
        color: danger ? 'var(--badge-red-text)' : 'var(--text-dim)',
        cursor: 'pointer',
        flexShrink: 0,
      }}
    >
      {children}
    </button>
  )
}

const inputStyle: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  height: 30,
  border: '1px solid var(--border)',
  borderRadius: 6,
  background: 'var(--input-bg)',
  color: 'var(--text)',
  fontSize: 13,
  fontFamily: 'inherit',
  outline: 'none',
  padding: '0 9px',
}
