import { create } from 'zustand'
import { getToken, setToken, clearToken, api } from '@/api/client'

interface AuthState {
  token: string | null
  email: string | null
  isAdmin: boolean
  impersonatorEmail: string | null
  setAuth: (token: string) => void
  logout: () => void
  fetchMe: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set) => ({
  token: getToken(),
  email: null,
  isAdmin: false,
  impersonatorEmail: null,
  setAuth: (token) => {
    setToken(token)
    set({ token, email: null, isAdmin: false, impersonatorEmail: null })
  },
  logout: () => {
    // Best-effort server-side revocation before dropping the token locally, so
    // a copied bearer stops working instead of staying valid for 7 days.
    api.auth.logout().catch(() => {})
    clearToken()
    set({ token: null, email: null, isAdmin: false, impersonatorEmail: null })
  },
  fetchMe: async () => {
    try {
      const me = await api.auth.me()
      set({
        email: me.email,
        isAdmin: me.is_admin,
        impersonatorEmail: me.impersonator_email,
      })
    } catch {
      set({ email: null, isAdmin: false, impersonatorEmail: null })
    }
  },
}))
