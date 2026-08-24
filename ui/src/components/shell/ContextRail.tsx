import { Link, useLocation } from 'react-router-dom'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { NAV_GROUPS, isActive } from './navigation'
import { useConfigStore } from '@/stores/config'
import { useCatalogStore } from '@/stores/catalog'
import { SutrButton } from '@/components/sutr'

export function ContextRail({
  collapsed,
  onToggleCollapsed,
  open,
  onNavigate,
}: {
  collapsed: boolean
  onToggleCollapsed: () => void
  /** Drawer state — only meaningful below the 900px breakpoint. */
  open: boolean
  onNavigate: () => void
}) {
  const { pathname } = useLocation()
  const isSelfHosted = useConfigStore((s) => s.isSelfHosted)
  const pendingApprovals = useCatalogStore((s) => s.pendingApprovals.length)

  return (
    <nav
      className="shell__rail"
      data-collapsed={collapsed}
      data-open={open}
      aria-label="Console sections"
    >
      {NAV_GROUPS.map((group) => {
        const items = group.items.filter((item) => !(item.cloudOnly && isSelfHosted))
        if (items.length === 0) return null
        return (
          <div className="shell__rail-group" key={group.id}>
            {group.label && !collapsed ? (
              <span className="shell__rail-label">{group.label}</span>
            ) : null}
            {items.map((item) => {
              const Icon = item.icon
              const active = isActive(item, pathname)
              const badge = item.to === '/app/approvals' && pendingApprovals > 0
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  className="shell__rail-item"
                  aria-current={active ? 'page' : undefined}
                  title={collapsed ? item.label : undefined}
                  onClick={onNavigate}
                >
                  <Icon size={15} />
                  {collapsed ? null : <span className="sutr-truncate">{item.label}</span>}
                  {badge && !collapsed ? (
                    <span className="shell__rail-count">{pendingApprovals}</span>
                  ) : null}
                </Link>
              )
            })}
          </div>
        )
      })}

      <div className="shell__rail-foot">
        <SutrButton
          variant="ghost"
          size="sm"
          iconOnly
          onClick={onToggleCollapsed}
          aria-label={collapsed ? 'Expand navigation' : 'Collapse navigation'}
          title={collapsed ? 'Expand navigation' : 'Collapse navigation'}
        >
          {collapsed ? <PanelLeftOpen size={15} /> : <PanelLeftClose size={15} />}
        </SutrButton>
      </div>
    </nav>
  )
}
