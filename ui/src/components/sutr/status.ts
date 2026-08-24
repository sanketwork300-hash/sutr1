/**
 * The operational vocabulary of the console.
 *
 * Every status the backend can report maps to exactly one label and one tone.
 * Labels are the operator's words — RUNNING, DENIED, APPROVAL REQUIRED — never
 * "Something went wrong" or "Working...". Nothing here invents a state the
 * backend has not reported: `running` is RUNNING, not HEALTHY, because a
 * running container is what the provider actually confirmed.
 */

export type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info' | 'brand'

export interface StatusDescriptor {
  label: string
  tone: Tone
  /** Transitional states pulse, so a queue that is moving looks like it is. */
  live?: boolean
}

const DEPLOYMENT: Record<string, StatusDescriptor> = {
  queued: { label: 'QUEUED', tone: 'info', live: true },
  building: { label: 'BUILDING', tone: 'info', live: true },
  deploying: { label: 'DEPLOYING', tone: 'info', live: true },
  running: { label: 'RUNNING', tone: 'success' },
  stopped: { label: 'STOPPED', tone: 'neutral' },
  failed: { label: 'FAILED', tone: 'danger' },
}

const APPROVAL: Record<string, StatusDescriptor> = {
  pending: { label: 'APPROVAL REQUIRED', tone: 'warning', live: true },
  approved: { label: 'APPROVED', tone: 'success' },
  auto_approved: { label: 'AUTO APPROVED', tone: 'success' },
  consumed: { label: 'CONSUMED', tone: 'neutral' },
  denied: { label: 'DENIED', tone: 'danger' },
  expired: { label: 'EXPIRED', tone: 'neutral' },
}

const OUTCOME: Record<string, StatusDescriptor> = {
  executed: { label: 'EXECUTED', tone: 'success' },
  approved: { label: 'APPROVED', tone: 'success' },
  approval_required: { label: 'APPROVAL REQUIRED', tone: 'warning' },
  denied: { label: 'DENIED', tone: 'danger' },
  error: { label: 'ERROR', tone: 'danger' },
  pending: { label: 'PENDING', tone: 'info', live: true },
}

const CONNECTION: Record<string, StatusDescriptor> = {
  connected: { label: 'CONNECTED', tone: 'success' },
  disconnected: { label: 'DISCONNECTED', tone: 'neutral' },
  not_configured: { label: 'NOT CONFIGURED', tone: 'neutral' },
  expired: { label: 'NEEDS REAUTHORIZATION', tone: 'warning' },
  unavailable: { label: 'UNAVAILABLE', tone: 'neutral' },
}

const EXECUTION_MODE: Record<string, StatusDescriptor> = {
  allow: { label: 'AUTO APPROVE', tone: 'success' },
  require_approval: { label: 'ASK', tone: 'warning' },
  deny: { label: 'DENY', tone: 'danger' },
}

const REGISTRY = {
  deployment: DEPLOYMENT,
  approval: APPROVAL,
  outcome: OUTCOME,
  connection: CONNECTION,
  mode: EXECUTION_MODE,
} as const

export type StatusDomain = keyof typeof REGISTRY

/** Unknown values are shown verbatim rather than hidden behind a guess. */
export function describeStatus(
  domain: StatusDomain,
  value: string | null | undefined,
): StatusDescriptor {
  if (!value) return { label: 'UNKNOWN', tone: 'neutral' }
  const found = REGISTRY[domain][value]
  if (found) return found
  return { label: value.replace(/_/g, ' ').toUpperCase(), tone: 'neutral' }
}

/**
 * Risk of a generated tool, derived from the HTTP verb the operation compiles
 * from. This is the only signal the compiler exposes today, so it is the only
 * one claimed — nothing here pretends to have read the upstream's semantics.
 */
export function riskForMethod(method: string): { label: string; tone: Tone } {
  switch (method.toUpperCase()) {
    case 'GET':
    case 'HEAD':
    case 'OPTIONS':
      return { label: 'low', tone: 'neutral' }
    case 'POST':
    case 'PUT':
    case 'PATCH':
      return { label: 'high', tone: 'warning' }
    case 'DELETE':
      return { label: 'critical', tone: 'danger' }
    default:
      return { label: 'unknown', tone: 'neutral' }
  }
}
