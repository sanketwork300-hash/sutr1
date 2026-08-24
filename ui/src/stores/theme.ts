import { create } from 'zustand'

type Theme = 'light' | 'dark'

interface ThemeState {
  theme: Theme
  toggle: () => void
}

function applyTheme(theme: Theme) {
  document.documentElement.setAttribute('data-theme', theme)
  localStorage.setItem('sutr_theme', theme)
}

// Sutr is a dark-first control plane: dark is the design reference, so a
// first-time visitor gets it unless they have previously chosen otherwise.
const saved = localStorage.getItem('sutr_theme')
const stored: Theme = saved === 'light' || saved === 'dark' ? saved : 'dark'
applyTheme(stored)

export const useThemeStore = create<ThemeState>((set) => ({
  theme: stored,
  toggle: () =>
    set((s) => {
      const next = s.theme === 'light' ? 'dark' : 'light'
      applyTheme(next)
      return { theme: next }
    }),
}))
