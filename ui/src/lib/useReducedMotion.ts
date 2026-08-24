import { useMediaQuery } from './useMediaQuery'

/**
 * Motion in Sutr explains infrastructure; when the viewer has asked for less
 * of it, the diagrams still render — they simply stop moving.
 */
export function useReducedMotion(): boolean {
  return useMediaQuery('(prefers-reduced-motion: reduce)')
}
