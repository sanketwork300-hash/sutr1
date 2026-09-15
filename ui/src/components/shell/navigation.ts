import {
  Activity,
  BarChart3,
  Bot,
  Boxes,
  BookOpen,
  Coins,
  CreditCard,
  FileJson,
  FlaskConical,
  GitBranch,
  HeartPulse,
  KeyRound,
  LayoutDashboard,
  LineChart,
  Library,
  Package,
  Radio,
  Search,
  Server,
  Store,
  Plug,
  Rocket,
  ScrollText,
  Settings,
  ShieldCheck,
  ShieldQuestion,
  Terminal,
  Users,
  Wrench,
} from 'lucide-react'
import type { ComponentType } from 'react'

export interface NavItem {
  to: string
  label: string
  icon: ComponentType<{ size?: number }>
  /** Matches child routes too, so a detail page keeps its parent highlighted. */
  match?: (pathname: string) => boolean
  /** Hidden on self-hosted installs, which have no billing. */
  cloudOnly?: boolean
}

export interface NavGroup {
  id: string
  label: string | null
  items: NavItem[]
}

const startsWith = (prefix: string) => (pathname: string) =>
  pathname === prefix || pathname.startsWith(`${prefix}/`)

/**
 * Progressive disclosure by intent, not by database table: what you build,
 * what you operate, what you govern, what you develop against. Every entry
 * here resolves to a real route backed by a real API.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    id: 'root',
    label: null,
    items: [{ to: '/app', label: 'Overview', icon: LayoutDashboard }],
  },
  {
    id: 'build',
    label: 'Build',
    items: [
      {
        to: '/app/marketplace',
        label: 'Marketplace',
        icon: Store,
        match: startsWith('/app/marketplace'),
      },
      { to: '/app/registry', label: 'Registry', icon: Library, match: startsWith('/app/registry') },
      { to: '/app/discovery', label: 'Discovery', icon: Search, match: startsWith('/app/discovery') },
      { to: '/app/apis', label: 'APIs', icon: FileJson, match: startsWith('/app/apis') },
      { to: '/app/sources', label: 'Sources', icon: GitBranch, match: startsWith('/app/sources') },
      {
        to: '/app/documentation',
        label: 'Documentation',
        icon: BookOpen,
        match: startsWith('/app/documentation'),
      },
      { to: '/app/runtimes', label: 'Runtimes', icon: Package, match: startsWith('/app/runtimes') },
      { to: '/app/mcp-servers', label: 'MCP Servers', icon: Boxes },
      {
        to: '/app/integrations',
        label: 'Integrations',
        icon: Plug,
        match: startsWith('/app/integrations'),
      },
      { to: '/app/tools', label: 'Tools', icon: Wrench, match: startsWith('/app/tools') },
    ],
  },
  {
    id: 'operate',
    label: 'Operate',
    items: [
      { to: '/app/playground', label: 'Playground', icon: FlaskConical },
      { to: '/app/approvals', label: 'Approvals', icon: ShieldQuestion },
      { to: '/app/activity', label: 'Activity', icon: Activity },
      { to: '/app/telemetry', label: 'Telemetry', icon: LineChart, match: startsWith('/app/telemetry') },
      { to: '/app/events', label: 'Events', icon: Radio, match: startsWith('/app/events') },
      { to: '/app/resilience', label: 'Resilience', icon: HeartPulse, match: startsWith('/app/resilience') },
      { to: '/app/platform', label: 'Platform', icon: Server, match: startsWith('/app/platform') },
      {
        to: '/app/deployments',
        label: 'Deployments',
        icon: Rocket,
        match: startsWith('/app/deployments'),
      },
    ],
  },
  {
    id: 'govern',
    label: 'Govern',
    items: [
      { to: '/app/policies', label: 'Policies', icon: ShieldCheck },
      {
        to: '/app/governance',
        label: 'Governance',
        icon: ShieldCheck,
        match: startsWith('/app/governance'),
      },
      { to: '/app/agents', label: 'Agents', icon: Bot, match: startsWith('/app/agents') },
      { to: '/app/access', label: 'Access', icon: Users },
      { to: '/app/credentials', label: 'Credentials', icon: KeyRound },
    ],
  },
  {
    id: 'develop',
    label: 'Develop',
    items: [
      { to: '/app/api-keys', label: 'API Keys', icon: KeyRound },
      { to: '/app/connect', label: 'Connect', icon: Terminal },
      { to: '/app/sdk', label: 'SDK', icon: ScrollText },
      { to: '/app/cli', label: 'CLI', icon: Terminal },
    ],
  },
  {
    id: 'account',
    label: null,
    items: [
      { to: '/app/usage', label: 'Usage', icon: BarChart3 },
      { to: '/app/revenue', label: 'Revenue', icon: Coins, match: startsWith('/app/revenue') },
      { to: '/app/settings', label: 'Settings', icon: Settings },
      { to: '/app/settings/billing', label: 'Billing', icon: CreditCard, cloudOnly: true },
    ],
  },
]

export function isActive(item: NavItem, pathname: string): boolean {
  if (item.match) return item.match(pathname)
  return pathname === item.to
}
