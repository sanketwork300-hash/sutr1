import { Link } from 'react-router-dom'
import { HelpCircle, Menu, Search } from 'lucide-react'
import { BrandLockup } from './BrandMark'
import { WorkspaceSwitcher } from './WorkspaceSwitcher'
import { EnvironmentIndicator } from './EnvironmentIndicator'
import { UserMenu } from './UserMenu'
import { SutrButton, SutrKbd } from '@/components/sutr'

export function TopBar({
  onOpenPalette,
  onOpenRail,
}: {
  onOpenPalette: () => void
  /** Present only on narrow viewports, where the rail is a drawer. */
  onOpenRail?: () => void
}) {
  const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || '')

  return (
    <header className="shell__top">
      {onOpenRail ? (
        <SutrButton
          variant="ghost"
          size="sm"
          iconOnly
          onClick={onOpenRail}
          aria-label="Open navigation"
        >
          <Menu size={16} />
        </SutrButton>
      ) : null}

      <Link to="/app" className="shell__brand" aria-label="Swaraj Sutr — overview">
        <BrandLockup size={18} />
      </Link>

      <span className="shell__divider" aria-hidden="true" />

      <WorkspaceSwitcher />

      <button type="button" className="shell__search" onClick={onOpenPalette}>
        <Search size={13} style={{ flexShrink: 0 }} />
        <span className="sutr-truncate">Search tools, integrations, deployments…</span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 3 }}>
          <SutrKbd>{isMac ? '⌘' : 'Ctrl'}</SutrKbd>
          <SutrKbd>K</SutrKbd>
        </span>
      </button>

      <div className="shell__top-right">
        <EnvironmentIndicator />
        <Link
          to="/app/connect"
          className="sutr-btn sutr-btn--ghost sutr-btn--sm sutr-btn--icon"
          aria-label="Connect an MCP client"
          title="Connect an MCP client"
        >
          <HelpCircle size={15} />
        </Link>
        <UserMenu />
      </div>
    </header>
  )
}
