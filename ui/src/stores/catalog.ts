import { create } from 'zustand'
import {
  api,
  type ApprovalRequest,
  type BundledIntegration,
  type Deployment,
  type InstalledIntegration,
  type OpenApiProject,
  type Tool,
} from '@/api/client'

/**
 * The searchable inventory of the workspace: everything the command palette
 * can jump to and everything the overview counts.
 *
 * Loaded on demand — never at app boot — because the tool catalog can be
 * large. Each surface that needs it calls `load()`, which is a no-op once the
 * data is in hand; `refresh()` forces a re-read after a mutation.
 */
interface CatalogState {
  tools: Tool[]
  integrations: BundledIntegration[]
  installed: InstalledIntegration[]
  deployments: Deployment[]
  projects: OpenApiProject[]
  pendingApprovals: ApprovalRequest[]
  loaded: boolean
  loading: boolean
  error: string | null
  load: () => Promise<void>
  refresh: () => Promise<void>
  /** Cheap poll for the rail badge — no catalog fetch involved. */
  loadPendingApprovals: () => Promise<void>
}

async function read() {
  // Settled rather than all: one failing resource (a provider that is down,
  // a permission the member lacks) must not blank the entire console.
  const [tools, integrations, installed, deployments, projects, approvals] =
    await Promise.allSettled([
      api.tools.listAll(),
      api.integrations.list(),
      api.installed.list(),
      api.deployments.list(),
      api.openapi.list(),
      api.approvals.list({ status: 'pending', limit: 100 }),
    ])

  const value = <T>(r: PromiseSettledResult<T>, fallback: T): T =>
    r.status === 'fulfilled' ? r.value : fallback

  const failure = [tools, integrations, installed, deployments, projects, approvals].find(
    (r): r is PromiseRejectedResult => r.status === 'rejected',
  )

  return {
    tools: value(tools, [] as Tool[]),
    integrations: value(integrations, [] as BundledIntegration[]),
    installed: value(installed, [] as InstalledIntegration[]),
    deployments: value(deployments, [] as Deployment[]),
    projects: value(projects, [] as OpenApiProject[]),
    pendingApprovals: value(approvals, [] as ApprovalRequest[]),
    error:
      failure && failure.reason instanceof Error
        ? failure.reason.message
        : failure
          ? 'Part of the workspace inventory could not be read.'
          : null,
  }
}

export const useCatalogStore = create<CatalogState>((set, get) => ({
  tools: [],
  integrations: [],
  installed: [],
  deployments: [],
  projects: [],
  pendingApprovals: [],
  loaded: false,
  loading: false,
  error: null,

  load: async () => {
    if (get().loaded || get().loading) return
    await get().refresh()
  },

  loadPendingApprovals: async () => {
    try {
      set({ pendingApprovals: await api.approvals.list({ status: 'pending', limit: 100 }) })
    } catch {
      // A badge is not worth surfacing an error for; the Approvals page will.
    }
  },

  refresh: async () => {
    set({ loading: true })
    try {
      set({ ...(await read()), loaded: true })
    } catch (error) {
      set({
        error: error instanceof Error ? error.message : 'Workspace inventory unavailable.',
        loaded: true,
      })
    } finally {
      set({ loading: false })
    }
  },
}))
