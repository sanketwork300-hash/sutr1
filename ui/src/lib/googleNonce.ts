/** sessionStorage slot for the nonce a Google sign-in was started with. */
export const GOOGLE_NONCE_KEY = 'sutr.google_nonce'

/**
 * Compare the nonce the callback returned with the one this tab started with,
 * and spend the stored one either way. Returns true only for a match.
 */
export function consumeGoogleNonce(returned: string | undefined): boolean {
  let stored: string | null = null
  try {
    stored = window.sessionStorage.getItem(GOOGLE_NONCE_KEY)
    window.sessionStorage.removeItem(GOOGLE_NONCE_KEY)
  } catch {
    return false
  }
  if (!stored || !returned || stored.length < 32) return false
  if (stored.length !== returned.length) return false
  // Not secret once the redirect has happened, but a plain comparison is
  // still the honest one: a prefix match must not count.
  let diff = 0
  for (let i = 0; i < stored.length; i += 1) diff |= stored.charCodeAt(i) ^ returned.charCodeAt(i)
  return diff === 0
}
