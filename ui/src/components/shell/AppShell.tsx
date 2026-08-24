import { useCallback, useEffect, useState } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import './shell.css'
import { TopBar } from './TopBar'
import { ContextRail } from './ContextRail'
import { CommandPalette } from './CommandPalette'
import { ImpersonationBanner } from '@/components/layout/ImpersonationBanner'
import { useAuthStore } from '@/stores/auth'
import { useCatalogStore } from '@/stores/catalog'
import { useMediaQuery } from '@/lib/useMediaQuery'

const COLLAPSE_KEY = 'sutr_rail_collapsed'

/**
 * The command centre: a top command bar, a compact context rail, and the
 * workspace. Everything above the workspace is chrome that never changes as
 * the operator moves between screens.
 */
export function AppShell() {
  const token = useAuthStore((s) => s.token)
  const fetchMe = useAuthStore((s) => s.fetchMe)
  const loadPendingApprovals = useCatalogStore((s) => s.loadPendingApprovals)
  const { pathname, search } = useLocation()
  const isNarrow = useMediaQuery('(max-width: 900px)')

  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSE_KEY) === 'true')
  const [railOpen, setRailOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)

  useEffect(() => {
    if (token) {
      void fetchMe()
      void loadPendingApprovals()
    }
  }, [token, fetchMe, loadPendingApprovals])

  // Keep the approvals badge honest as the operator works.
  useEffect(() => {
    if (!token) return
    void loadPendingApprovals()
  }, [pathname, token, loadPendingApprovals])

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const toggleCollapsed = useCallback(() => {
    setCollapsed((value) => {
      localStorage.setItem(COLLAPSE_KEY, String(!value))
      return !value
    })
  }, [])

  if (!token) {
    const target = `${pathname}${search}`
    return <Navigate to={`/login?redirect=${encodeURIComponent(target)}`} replace />
  }

  return (
    <div className="shell">
      <TopBar
        onOpenPalette={() => setPaletteOpen(true)}
        onOpenRail={isNarrow ? () => setRailOpen(true) : undefined}
      />

      {isNarrow && railOpen ? (
        <div className="shell__rail-scrim" onClick={() => setRailOpen(false)} aria-hidden="true" />
      ) : null}

      <ContextRail
        collapsed={!isNarrow && collapsed}
        onToggleCollapsed={toggleCollapsed}
        open={isNarrow ? railOpen : false}
        onNavigate={() => setRailOpen(false)}
      />

      <div className="shell__work">
        <ImpersonationBanner />
        <Outlet />
      </div>

      {/* Keyed so each opening starts from a clean query and cursor. */}
      <CommandPalette
        key={paletteOpen ? 'palette-open' : 'palette-closed'}
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
      />
    </div>
  )
}
