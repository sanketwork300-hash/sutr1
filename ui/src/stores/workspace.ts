import { create } from 'zustand'
import { api, type OrgInfo, type Workspace } from '@/api/client'

const ACTIVE_KEY = 'sutr_active_workspace'

interface WorkspaceState {
  org: OrgInfo | null
  workspaces: Workspace[]
  activeId: string | null
  loaded: boolean
  load: () => Promise<void>
  refresh: () => Promise<void>
  setActive: (id: string) => void
}

/**
 * Organisation identity and the workspace list behind the switcher.
 *
 * The selected workspace is a console-side preference: the REST surface is
 * organisation-scoped today, so nothing here filters what the API returns.
 * The switcher says so rather than implying an isolation the server does not
 * yet enforce.
 */
export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  org: null,
  workspaces: [],
  activeId: localStorage.getItem(ACTIVE_KEY),
  loaded: false,

  load: async () => {
    if (get().loaded) return
    await get().refresh()
  },

  refresh: async () => {
    const [org, workspaces] = await Promise.allSettled([api.org.get(), api.workspaces.list()])
    const list = workspaces.status === 'fulfilled' ? workspaces.value : []
    const stored = get().activeId
    const active = list.find((w) => w.id === stored) ?? list.find((w) => w.is_default) ?? list[0]
    set({
      org: org.status === 'fulfilled' ? org.value : null,
      workspaces: list,
      activeId: active?.id ?? null,
      loaded: true,
    })
  },

  setActive: (id) => {
    localStorage.setItem(ACTIVE_KEY, id)
    set({ activeId: id })
  },
}))
