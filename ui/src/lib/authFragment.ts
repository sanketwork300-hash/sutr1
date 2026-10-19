/**
 * Take the Google sign-in result out of the URL before anything else runs.
 *
 * The server lands the browser on `/login/google/callback#access_token=...`.
 * The page that consumes that fragment is lazy-loaded, so by the time it
 * mounts the URL has been sitting in the address bar, history, and anything
 * that reads `location.href` for long enough to be captured. This module is
 * imported first from `main.tsx`, so it runs synchronously at boot: the
 * fragment is moved into memory and the address bar is rewritten to the bare
 * path before React, the router, or analytics exist.
 */
export const GOOGLE_CALLBACK_PATH = '/login/google/callback'

let stashed: string | null = null

if (typeof window !== 'undefined' && window.location.pathname === GOOGLE_CALLBACK_PATH) {
  const hash = window.location.hash
  if (hash.length > 1) {
    stashed = hash.slice(1)
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
  }
}

/** The fragment the callback arrived with, without its leading `#`. */
export function readGoogleCallbackFragment(): string | null {
  return stashed
}

/** Forget the fragment once the token has been handed to the auth store. */
export function clearGoogleCallbackFragment(): void {
  stashed = null
}

/** The only fields the callback reads from its fragment. */
const FRAGMENT_KEYS = ['access_token', 'totp_challenge', 'nonce', 'error'] as const

export type CallbackFragment = Partial<Record<(typeof FRAGMENT_KEYS)[number], string>>

/**
 * The fragment is whatever the link that opened this page says, so its keys
 * are never used as property names: pairs go into a Map, and only the known
 * fields are copied out of it.
 */
export function parseFragment(fragment: string | null): CallbackFragment {
  const pairs = new Map<string, string>()
  if (fragment) {
    for (const part of fragment.split('&')) {
      const index = part.indexOf('=')
      const key = index === -1 ? part : part.slice(0, index)
      const value = index === -1 ? '' : part.slice(index + 1)
      if (!key) continue
      try {
        pairs.set(decodeURIComponent(key), decodeURIComponent(value))
      } catch {
        // A malformed escape is not ours; skip the pair rather than throw.
      }
    }
  }
  const out: CallbackFragment = {}
  for (const key of FRAGMENT_KEYS) {
    const value = pairs.get(key)
    if (value !== undefined) out[key] = value
  }
  return out
}
