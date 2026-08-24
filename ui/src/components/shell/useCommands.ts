import { useMemo } from 'react'
import { useCatalogStore } from '@/stores/catalog'

export interface Command {
  id: string
  group: string
  label: string
  hint?: string
  keywords?: string
  /** Every command resolves to a real route — nothing here is decorative. */
  to: string
}

const ACTIONS: Command[] = [
  { id: 'nav-overview', group: 'Navigate', label: 'Overview', to: '/app' },
  { id: 'act-import', group: 'Create', label: 'Import OpenAPI specification', to: '/app/apis/new' },
  {
    id: 'act-mcp',
    group: 'Create',
    label: 'Create MCP server',
    hint: 'From an OpenAPI specification',
    to: '/app/apis/new',
  },
  { id: 'act-connect', group: 'Create', label: 'Connect an integration', to: '/app/integrations' },
  { id: 'act-key', group: 'Create', label: 'Create API key', to: '/app/api-keys' },
  { id: 'nav-tools', group: 'Navigate', label: 'Tools', to: '/app/tools' },
  { id: 'nav-playground', group: 'Navigate', label: 'Playground', to: '/app/playground' },
  { id: 'nav-approvals', group: 'Navigate', label: 'Approvals', to: '/app/approvals' },
  { id: 'nav-activity', group: 'Navigate', label: 'Activity', to: '/app/activity' },
  { id: 'nav-deployments', group: 'Navigate', label: 'Deployments', to: '/app/deployments' },
  { id: 'nav-apis', group: 'Navigate', label: 'APIs', to: '/app/apis' },
  { id: 'nav-mcp', group: 'Navigate', label: 'MCP Servers', to: '/app/mcp-servers' },
  { id: 'nav-policies', group: 'Navigate', label: 'Policies', to: '/app/policies' },
  { id: 'nav-access', group: 'Navigate', label: 'Access', to: '/app/access' },
  { id: 'nav-credentials', group: 'Navigate', label: 'Credentials', to: '/app/credentials' },
  { id: 'nav-usage', group: 'Navigate', label: 'Usage', to: '/app/usage' },
  { id: 'nav-sdk', group: 'Navigate', label: 'SDK', to: '/app/sdk' },
  { id: 'nav-cli', group: 'Navigate', label: 'CLI', to: '/app/cli' },
  { id: 'nav-connect', group: 'Navigate', label: 'Connect an MCP client', to: '/app/connect' },
  { id: 'nav-settings', group: 'Navigate', label: 'Settings', to: '/app/settings' },
  {
    id: 'nav-workspaces',
    group: 'Navigate',
    label: 'Switch workspace',
    to: '/app/access?section=workspaces',
  },
]

function score(command: Command, query: string): number {
  if (!query) return 1
  const haystack = `${command.label} ${command.keywords ?? ''} ${command.hint ?? ''}`.toLowerCase()
  const needle = query.toLowerCase()
  const index = haystack.indexOf(needle)
  if (index < 0) return 0
  // Prefix matches outrank matches buried in the middle of a description.
  return command.label.toLowerCase().startsWith(needle) ? 3 : index === 0 ? 2 : 1
}

/**
 * Static actions plus whatever the workspace actually contains. Tools,
 * integrations, deployments and API projects become jump targets once the
 * catalog has loaded; before then the palette still navigates.
 */
export function useCommands(query: string): Command[] {
  const tools = useCatalogStore((s) => s.tools)
  const integrations = useCatalogStore((s) => s.integrations)
  const deployments = useCatalogStore((s) => s.deployments)
  const projects = useCatalogStore((s) => s.projects)

  const inventory = useMemo<Command[]>(() => {
    const items: Command[] = []

    for (const integration of integrations) {
      items.push({
        id: `integration-${integration.id}`,
        group: 'Integrations',
        label: integration.name,
        hint: integration.type === 'remote_mcp' ? 'MCP server' : 'API integration',
        keywords: `${integration.id} ${integration.description ?? ''}`,
        to: `/app/integrations/${encodeURIComponent(integration.id)}`,
      })
    }

    for (const tool of tools) {
      items.push({
        id: `tool-${tool.integration_id}-${tool.name}`,
        group: 'Tools',
        label: tool.name,
        hint: tool.integration_id,
        keywords: tool.description ?? '',
        to: `/app/tools?integration=${encodeURIComponent(
          tool.integration_id ?? '',
        )}&tool=${encodeURIComponent(tool.name)}`,
      })
    }

    for (const deployment of deployments) {
      items.push({
        id: `deployment-${deployment.id}`,
        group: 'Deployments',
        label: deployment.name,
        hint: `${deployment.provider} · ${deployment.status}`,
        to: `/app/deployments?deployment=${encodeURIComponent(deployment.id)}`,
      })
    }

    for (const project of projects) {
      items.push({
        id: `project-${project.id}`,
        group: 'APIs',
        label: project.api_title || project.name,
        hint: `${project.operation_count} operations`,
        keywords: project.name,
        to: `/app/apis?project=${encodeURIComponent(project.id)}`,
      })
    }

    return items
  }, [tools, integrations, deployments, projects])

  return useMemo(() => {
    const all = [...ACTIONS, ...inventory]
    const trimmed = query.trim()
    return all
      .map((command) => ({ command, rank: score(command, trimmed) }))
      .filter((entry) => entry.rank > 0)
      .sort((a, b) => b.rank - a.rank)
      .slice(0, trimmed ? 40 : 14)
      .map((entry) => entry.command)
  }, [inventory, query])
}
