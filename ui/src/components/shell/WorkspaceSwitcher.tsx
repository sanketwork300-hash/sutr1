import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, ChevronsUpDown } from 'lucide-react'
import { useWorkspaceStore } from '@/stores/workspace'

export function WorkspaceSwitcher() {
  const { org, workspaces, activeId, load, setActive } = useWorkspaceStore()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!open) return
    function onDocumentClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDocumentClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocumentClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const active = workspaces.find((w) => w.id === activeId)

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button
        type="button"
        className="shell__workspace"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'flex-start',
            minWidth: 0,
            lineHeight: 1.15,
          }}
        >
          <span className="shell__workspace-org sutr-truncate" style={{ maxWidth: 160 }}>
            {org?.name ?? 'Organisation'}
          </span>
          <span className="sutr-truncate" style={{ maxWidth: 160, fontWeight: 500 }}>
            {active?.name ?? 'Default'}
          </span>
        </span>
        <ChevronsUpDown size={13} style={{ color: 'var(--text-faint)', flexShrink: 0 }} />
      </button>

      {open ? (
        <div
          className="sutr-popover"
          role="menu"
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            left: 0,
            minWidth: 260,
            zIndex: 130,
          }}
        >
          <div className="sutr-menu-label">Workspaces</div>
          {workspaces.length === 0 ? (
            <div style={{ padding: '6px 12px 10px' }} className="sutr-meta">
              No workspaces returned.
            </div>
          ) : (
            workspaces.map((workspace) => (
              <button
                key={workspace.id}
                type="button"
                role="menuitem"
                className="sutr-menu-item"
                onClick={() => {
                  setActive(workspace.id)
                  setOpen(false)
                }}
              >
                <span className="sutr-truncate">{workspace.name}</span>
                {workspace.is_default ? (
                  <span className="sutr-meta" style={{ marginLeft: 'auto' }}>
                    default
                  </span>
                ) : null}
                {workspace.id === activeId ? (
                  <Check
                    size={13}
                    style={{ color: 'var(--brand)', marginLeft: workspace.is_default ? 6 : 'auto' }}
                  />
                ) : null}
              </button>
            ))
          )}
          <div
            style={{
              borderTop: '1px solid var(--border)',
              padding: '8px 12px',
              display: 'flex',
              flexDirection: 'column',
              gap: 6,
            }}
          >
            <span className="sutr-meta">
              Workspaces name and group your infrastructure. Console data is organisation-scoped.
            </span>
            <Link
              to="/app/access"
              className="sutr-menu-item"
              style={{ padding: 0, fontSize: 12, color: 'var(--brand)' }}
              onClick={() => setOpen(false)}
              role="menuitem"
            >
              Manage workspaces and access →
            </Link>
          </div>
        </div>
      ) : null}
    </div>
  )
}
