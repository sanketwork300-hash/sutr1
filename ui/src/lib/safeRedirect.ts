/**
 * Reduce a `?redirect=` value to a path inside this app, or to the fallback.
 *
 * The router accepts anything that looks like a path, and the browser resolves
 * some of those somewhere else: `/\evil.com` becomes `https://evil.com/`, and
 * `//evil.com` is protocol-relative. A sign-in link carrying either would send
 * the user off-site right after a genuine login, so only a plain absolute
 * path that resolves to our own origin survives; everything else is replaced,
 * never repaired.
 */
export function safeInternalPath(
  value: string | null | undefined,
  fallback = '/app',
  origin: string = typeof window !== 'undefined' ? window.location.origin : 'http://localhost',
): string {
  if (typeof value !== 'string' || !value) return fallback
  if (!isPlainPath(value)) return fallback

  let resolved: URL
  try {
    resolved = new URL(value, origin)
  } catch {
    return fallback
  }
  if (resolved.origin !== origin) return fallback

  // Return the normalised form so `/a/..//host` cannot come back as `//host`.
  const normalised = `${resolved.pathname}${resolved.search}${resolved.hash}`
  return isPlainPath(normalised) ? normalised : fallback
}

function isPlainPath(value: string): boolean {
  // One leading slash, not followed by another slash or a backslash, and no
  // backslash or control character anywhere.
  // eslint-disable-next-line no-control-regex
  return /^\/(?![/\\])/.test(value) && !/[\\\u0000-\u001f\u007f]/.test(value)
}
