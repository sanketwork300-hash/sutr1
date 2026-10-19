import posthog from 'posthog-js'
import { IS_CLOUD } from '@/lib/env'

/**
 * Analytics are opt-in and start late.
 *
 * Opt-in: the client only runs with a project key, taken from
 * `VITE_PUBLIC_POSTHOG_KEY`; the hosted build (IS_CLOUD) has one compiled in.
 * A self-hosted install that sets no key sends nothing anywhere.
 *
 * Late: `initAnalytics()` is called by the router, never at import time, and
 * never while the browser is on a route whose URL carries a credential. Page
 * views are captured by hand from the path alone, and every event's URL-like
 * property is reduced to a path before it leaves the browser, so a session
 * token or reset link can never ride along in `$current_url`.
 */

const posthogHost = import.meta.env.VITE_PUBLIC_POSTHOG_HOST ?? 'https://us.i.posthog.com'
const CLOUD_PROJECT_KEY = 'phc_o9LvxygZagFKt6J9UcJmzf4Ldrj2cFZx8mKMoAtuuUpb'

function projectKey(): string {
  const configured = String(import.meta.env.VITE_PUBLIC_POSTHOG_KEY ?? '').trim()
  if (configured) return configured
  return IS_CLOUD ? CLOUD_PROJECT_KEY : ''
}

/** Routes whose URL carries a token, a session, or a redirect target. */
const TOKEN_BEARING_ROUTES = [
  '/login/google/callback',
  '/reset-password',
  '/join',
  '/verify-email',
  '/oauth/authorize',
  '/oauth/success',
]

export function isTokenBearingRoute(pathname: string): boolean {
  return TOKEN_BEARING_ROUTES.some(
    (route) => pathname === route || pathname.startsWith(`${route}/`),
  )
}

const URL_PROPERTY = /^\$(initial_)?(current_url|referrer|pathname)$/
const SENSITIVE_PARAMS = ['token', 'session', 'redirect', 'nonce', 'code', 'state']

/** `https://host/path?x#y` → `https://host/path`; `/path?x` → `/path`. */
export function pathOnly(value: unknown): unknown {
  if (typeof value !== 'string') return value
  if (/^https?:\/\//i.test(value)) {
    try {
      const url = new URL(value)
      for (const param of SENSITIVE_PARAMS) url.searchParams.delete(param)
      return `${url.origin}${url.pathname}`
    } catch {
      return value.split(/[?#]/)[0]
    }
  }
  if (value.startsWith('/')) return value.split(/[?#]/)[0]
  // `$direct`, a bare domain, or anything that is not a URL: leave it.
  return value
}

type Properties = Record<string, unknown>

function scrubUrls(target: Properties): void {
  for (const key of Object.keys(target)) {
    if (URL_PROPERTY.test(key)) target[key] = pathOnly(target[key])
  }
}

export function sanitizeProperties(properties: Properties): Properties {
  scrubUrls(properties)
  for (const bucket of ['$set', '$set_once']) {
    const nested = properties[bucket]
    if (nested && typeof nested === 'object') scrubUrls(nested as Properties)
  }
  return properties
}

let initialised = false

export function isAnalyticsEnabled(): boolean {
  return initialised
}

/** Start the client. Safe to call more than once; a no-op without a key. */
export function initAnalytics(): boolean {
  if (initialised) return true
  const key = projectKey()
  if (!key) return false
  posthog.init(key, {
    api_host: posthogHost,
    defaults: '2026-01-30',
    person_profiles: 'identified_only',
    capture_pageview: false,
    sanitize_properties: (properties) => sanitizeProperties(properties as Properties),
  })
  initialised = true
  return true
}

export function capturePageview(pathname: string): void {
  if (!initialised) return
  posthog.capture('$pageview', {
    $current_url: `${window.location.origin}${pathname}`,
    $pathname: pathname,
  })
}

export { posthog }
