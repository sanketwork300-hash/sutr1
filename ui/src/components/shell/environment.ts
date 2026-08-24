/**
 * Which instance the operator is looking at, derived from the host it is
 * served from. There is no environment field on the API, so this reports the
 * one fact that is genuinely known rather than inventing a label.
 */
export type EnvironmentName = 'LOCAL' | 'STAGING' | 'PRODUCTION'

export function environmentName(hostname: string): EnvironmentName {
  if (hostname === 'localhost' || hostname === '127.0.0.1' || hostname.endsWith('.local')) {
    return 'LOCAL'
  }
  if (/(^|[.-])(staging|stage|dev|preview|test)([.-]|$)/.test(hostname)) return 'STAGING'
  return 'PRODUCTION'
}

export function currentEnvironment(): EnvironmentName {
  return environmentName(typeof window === 'undefined' ? '' : window.location.hostname)
}
