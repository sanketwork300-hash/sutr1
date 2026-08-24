import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { usePostHog } from '@posthog/react'
import { LogOut, Moon, Settings, Shield, Sun } from 'lucide-react'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'

function emailColor(email: string): string {
  let hash = 0
  for (let i = 0; i < email.length; i++) {
    hash = email.charCodeAt(i) + ((hash << 5) - hash)
  }
  return `hsl(${Math.abs(hash) % 360}, 42%, 46%)`
}

export function UserMenu() {
  const { email, isAdmin, logout } = useAuthStore()
  const { theme, toggle } = useThemeStore()
  const posthog = usePostHog()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

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

  function handleLogout() {
    posthog?.reset()
    logout()
    navigate('/login')
  }

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={email ? `Account: ${email}` : 'Account'}
        style={{
          width: 26,
          height: 26,
          borderRadius: 99,
          background: email ? emailColor(email) : 'var(--surface-hover)',
          border: 'none',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 11,
          fontWeight: 700,
          color: '#fff',
          cursor: 'pointer',
        }}
      >
        {email ? email[0].toUpperCase() : '?'}
      </button>

      {open ? (
        <div
          className="sutr-popover"
          role="menu"
          style={{
            position: 'absolute',
            top: 'calc(100% + 8px)',
            right: 0,
            minWidth: 214,
            zIndex: 130,
          }}
        >
          {email ? (
            <div
              style={{
                padding: '10px 12px',
                borderBottom: '1px solid var(--border)',
                fontSize: 12,
                color: 'var(--text-dim)',
                wordBreak: 'break-all',
              }}
            >
              {email}
            </div>
          ) : null}

          <button
            type="button"
            role="menuitem"
            className="sutr-menu-item"
            onClick={() => {
              setOpen(false)
              navigate('/app/settings')
            }}
          >
            <Settings size={14} /> Settings
          </button>

          <button type="button" role="menuitem" className="sutr-menu-item" onClick={toggle}>
            {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
            {theme === 'dark' ? 'Light appearance' : 'Dark appearance'}
          </button>

          {isAdmin ? (
            <button
              type="button"
              role="menuitem"
              className="sutr-menu-item"
              onClick={() => {
                setOpen(false)
                navigate('/app/admin')
              }}
            >
              <Shield size={14} /> Instance admin
            </button>
          ) : null}

          <div style={{ borderTop: '1px solid var(--border)' }}>
            <button
              type="button"
              role="menuitem"
              className="sutr-menu-item sutr-menu-item--danger"
              onClick={handleLogout}
            >
              <LogOut size={14} /> Log out
            </button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
