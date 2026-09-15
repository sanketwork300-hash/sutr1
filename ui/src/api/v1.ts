/**
 * The `/v1` surface (ESDS LLD §5.5).
 *
 * Separate from `client.ts` because the two surfaces are genuinely different,
 * not merely differently prefixed:
 *
 * - `/api` returns bare JSON bodies and is frozen — the published CLI and both
 *   SDKs read it.
 * - `/v1` wraps every success in `{data, meta}` and every failure in
 *   `{error, meta}`. Unwrapping that in one place means no page has to know
 *   the envelope exists, and `meta.correlation_id` is still available on an
 *   error, which is what makes a support report traceable.
 *
 * Nothing here invents a field. Where the platform returns a shape that is
 * genuinely open-ended — a policy document, a pricing context — the type says
 * `Record<string, unknown>` rather than a guess that will be wrong later.
 */

import { ApiError, clearToken, getToken } from '@/api/client'

interface Envelope<T> {
  data: T
  meta?: { request_id?: string; correlation_id?: string }
}

interface ErrorEnvelope {
  error?: {
    code?: string
    message?: string
    details?: Record<string, unknown>
    retry_after?: number | null
  }
  meta?: { request_id?: string; correlation_id?: string }
}

function messageFrom(body: ErrorEnvelope, status: number): string {
  return body?.error?.message ?? `Request failed: ${status}`
}

export async function requestV1<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const headers = new Headers(init?.headers)
  if (token && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')

  const res = await fetch(`/v1${path}`, { ...init, headers })

  if (res.status === 401) {
    clearToken()
    window.location.href = '/login'
    throw new Error('Unauthorized')
  }

  if (!res.ok) {
    const body: ErrorEnvelope = await res.json().catch(() => ({}))
    // The request id travels on `meta`, not on the error, so it is lifted onto
    // the thrown body where `describeError` already looks for it.
    throw new ApiError(
      res.status,
      { ...body, request_id: body?.meta?.request_id },
      messageFrom(body, res.status),
    )
  }

  if (res.status === 204) return undefined as T
  const body: Envelope<T> = await res.json()
  return body.data
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

const post = <T>(path: string, body?: unknown) =>
  requestV1<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
  })

/**
 * Some `/v1` collections answer with a bare array and others with an object
 * that names the collection alongside the service's own explanatory fields
 * (`/v1/billing/plans` returns `plans` next to `models`, `tiering`, `money`).
 * Both are reasonable; a page that assumes one and receives the other renders
 * an empty table and reports nothing, which is the worst possible failure —
 * indistinguishable from "you have none".
 *
 * So the collection is extracted rather than assumed, and a shape that matches
 * neither throws instead of quietly returning nothing.
 */
async function listOf<T>(path: string, key: string): Promise<T[]> {
  const body = await requestV1<unknown>(path)
  if (Array.isArray(body)) return body as T[]
  if (body && typeof body === 'object') {
    const named = (body as Record<string, unknown>)[key]
    if (Array.isArray(named)) return named as T[]
  }
  throw new Error(`${path} did not return a list of ${key}`)
}

// ── Shared shapes ────────────────────────────────────────────────────────────

/** Every `capabilities` endpoint answers the same question: what can this
 *  install actually do? The shape differs per service, so it stays open. */
export type Capabilities = Record<string, unknown>

export interface Page<T> {
  items?: T[]
  next_cursor?: string | null
  count?: number
}

// ── Registry (LLD §3.7) ──────────────────────────────────────────────────────

export interface RegistryTool {
  id: string
  tool_key: string
  name: string
  summary?: string
  /** The registry's own name for it. Not `state` — checked against a live
   *  response rather than assumed. */
  lifecycle_state: string
  state_reason?: string | null
  visibility?: string
  category?: string | null
  integration_id?: string | null
  current_version?: number | null
  published_version?: number | null
  tags?: string[]
  updated_at?: string
  created_at?: string
  [key: string]: unknown
}

export interface RegistryVersion {
  version: number
  state?: string
  created_at?: string
  notes?: string
  schema_hash?: string
  [key: string]: unknown
}

export interface TrustScore {
  score: number | null
  max?: number
  /** How much of the score could be computed at all. A component with no
   *  evidence is unavailable, not zero. */
  coverage?: number
  unavailable_reason?: string | null
  explanation?: string
  components?: Array<{
    name: string
    weight: number
    value: number | null
    points: number | null
    available: boolean
    unavailable_reason?: string | null
    detail?: Record<string, unknown>
  }>
}

export interface ChangeRequest {
  id: string
  tool_id: string
  status: string
  kind?: string
  requested_by?: string | null
  created_at?: string
  [key: string]: unknown
}

// ── Discovery (LLD §3.8) ─────────────────────────────────────────────────────

export interface DiscoveryStage {
  name: string
  duration_ms: number
  detail?: Record<string, unknown>
}

export interface DiscoveryDegradation {
  stage: string
  reason: string
  effect: string
}

export interface DiscoveryResult {
  intent: string
  results: Array<{
    tool_id?: string
    score?: number
    candidate?: Record<string, unknown>
    reasons?: unknown
    [key: string]: unknown
  }>
  suggestions?: unknown[]
  stages: DiscoveryStage[]
  degradations: DiscoveryDegradation[]
  ranking_version?: string
  stale_results?: number
  cached?: boolean
  latency_ms?: number
  filter_summary?: Record<string, unknown>
  retrieval_summary?: Record<string, unknown>
}

// ── Sources (LLD §3.3) ───────────────────────────────────────────────────────

export interface ApiSource {
  id: string
  connector: string
  label?: string
  role?: string
  status?: string
  last_checked_at?: string | null
  last_error?: string | null
  [key: string]: unknown
}

export interface DriftReport {
  id: string
  source_id?: string
  classification?: string
  summary?: string
  created_at?: string
  changes?: unknown[]
  [key: string]: unknown
}

// ── Documentation (LLD §3.5) ─────────────────────────────────────────────────

export interface DocumentRecord {
  id: string
  filename?: string
  title?: string
  status?: string
  content_type?: string
  bytes?: number
  created_at?: string
  [key: string]: unknown
}

// ── Governance (LLD §5.2) ────────────────────────────────────────────────────

export interface GovernancePolicy {
  id: string
  key?: string
  name: string
  description?: string
  active_version?: number | null
  versions?: Array<{ version: number; state: string; [key: string]: unknown }>
  [key: string]: unknown
}

export interface ComplianceRun {
  id: string
  framework?: string
  status?: string
  score?: number | null
  created_at?: string
  controls?: unknown[]
  [key: string]: unknown
}

export interface GovernanceReview {
  id: string
  tool_id?: string
  stage?: string
  status?: string
  created_at?: string
  [key: string]: unknown
}

export interface GovernanceException {
  id: string
  policy_id?: string
  state?: string
  reason?: string
  expires_at?: string | null
  [key: string]: unknown
}

// ── Provisioning (LLD §4.3) ──────────────────────────────────────────────────

export interface AgentIdentity {
  id: string
  name: string
  description?: string | null
  /** The platform's own word for what kind of caller this is. Checked against
   *  a live response — it is `kind`, not `agent_type`. */
  kind?: string
  principal?: string | null
  active: boolean
  created_at?: string
  last_seen_at?: string | null
  revoked_at?: string | null
  revoked_reason?: string | null
  /** Attributes a tenant declared about its own agent, and the platform says
   *  so rather than presenting them as verified. */
  attributes?: Record<string, unknown>
  attributes_are_tenant_declared?: boolean
}

export interface AccessPass {
  id: string
  agent_id?: string
  /** What the pass is scoped to. `resource` is the integration; `tools` are
   *  the specific tools within it — a pass is not a standing grant. */
  resource?: string
  tools?: string[]
  audience?: string
  purpose?: string | null
  status?: string
  issued_at?: string
  expires_at?: string | null
  first_seen_at?: string | null
  use_count?: number
  revoked_at?: string | null
  revoked_reason?: string | null
  decision?: Record<string, unknown>
}

// ── Billing (LLD §5.1) ───────────────────────────────────────────────────────

export interface PricingPlan {
  id: string
  key: string
  name: string
  model: string
  state?: string
  version?: number
  currency?: string
  amount_micros?: number
  included_units?: number
  [key: string]: unknown
}

export interface Invoice {
  id: string
  number?: string
  state: string
  currency: string
  period_start?: string
  period_end?: string
  subtotal_micros?: number
  total_micros?: number
  lines?: Array<Record<string, unknown>>
  unpriced?: Array<Record<string, unknown>>
  collected_by_this_platform?: boolean
  [key: string]: unknown
}

export interface LedgerEntry {
  id: string
  kind: string
  direction?: string
  amount_micros: number
  currency?: string
  balance_micros?: number
  reason?: string
  created_at?: string
  [key: string]: unknown
}

export interface Settlement {
  id: string
  state: string
  provider_org_id?: string
  provider_share_bps?: number
  provider_amount_micros?: number
  platform_amount_micros?: number
  paid_out_by_this_platform?: boolean
  [key: string]: unknown
}

// ── Observability (LLD §5.3) ─────────────────────────────────────────────────

export interface LatencySummary {
  count: number
  p50: number | null
  p95: number | null
  p99: number | null
  max: number | null
}

export interface TelemetrySummary {
  window: { start: string; end: string }
  calls: number
  truncated: boolean
  by_outcome: Record<string, number>
  errors: number
  error_ratio: number
  calls_per_minute: number
  latency_ms: LatencySummary
  by_provider: Record<string, { calls: number; latency_ms: LatencySummary }>
}

export interface TelemetryOperation {
  correlation_id: string
  steps: number
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  trace_id: string | null
}

export interface TelemetryTimeline {
  correlation_id: string
  trace_id: string | null
  steps: Array<{
    timestamp: string
    provider_id: string | null
    tool_id: string | null
    outcome: string | null
    duration_ms: number | null
    access_reason: string | null
    error: string | null
  }>
}

// ── Platform (LLD §5.4, §5.7) ────────────────────────────────────────────────

export interface PlatformCapability {
  name: string
  available: boolean
  reason: string | null
  detail: string
}

export interface PlatformMode {
  mode: string
  writes_allowed: boolean
  region: string | null
  primary_region: string | null
  read_only_writes: string[]
}

export interface Leadership {
  instance_id: string
  jobs: string[]
  held: string[]
  holders: Record<string, string | null>
  lease_seconds: number
  renew_seconds: number
}

export interface ScalingReport {
  process_local_state: Array<{
    name: string
    location: string
    severity: string
    effect: string
    remedy: string
  }>
  summary: string
}

// ── Resilience (LLD §5.8) ────────────────────────────────────────────────────

export interface ProviderHealth {
  provider: string
  state: string
  score: number | null
  successes: number
  failures: number
  consecutive_failures?: number
  last_error?: string | null
  quarantined: boolean
  seconds_in_state?: number
}

export interface ResiliencePolicy {
  timeouts: Record<string, number>
  retries: Record<string, unknown>
  circuit_breaker: Record<string, unknown>
  severities: Record<string, string>
  domains: Array<{
    name: string
    severity: string
    effect: string
    survives: string
    implemented_in: string
  }>
  not_implemented: Record<string, string>
}

// ── Events (LLD §5.6) ────────────────────────────────────────────────────────

export interface OutboxEventRow {
  event_id: string
  event_type: string
  state: string
  attempts?: number
  last_error?: string | null
  created_at?: string
  published_at?: string | null
  correlation_id?: string | null
  [key: string]: unknown
}

// ── Generation (LLD §3.6) ────────────────────────────────────────────────────

export interface RuntimeArtifact {
  id: string
  name?: string
  state?: string
  build_hash?: string
  signed?: boolean
  created_at?: string
  [key: string]: unknown
}

// ── The client ───────────────────────────────────────────────────────────────

export const v1 = {
  registry: {
    lifecycle: () => requestV1<Record<string, unknown>>('/registry/lifecycle'),
    tools: (params: { state?: string; visibility?: string; limit?: number } = {}) =>
      listOf<RegistryTool>(`/registry/tools${query(params)}`, 'tools'),
    tool: (id: string) => requestV1<RegistryTool>(`/registry/tools/${id}`),
    transition: (id: string, state: string, reason?: string) =>
      post<RegistryTool>(`/registry/tools/${id}/transition`, { state, reason }),
    deprecate: (id: string, reason?: string) =>
      post<RegistryTool>(`/registry/tools/${id}/deprecate`, { reason }),
    archive: (id: string, reason?: string) =>
      post<RegistryTool>(`/registry/tools/${id}/archive`, { reason }),
    versions: (id: string) => listOf<RegistryVersion>(`/registry/tools/${id}/versions`, 'versions'),
    trust: (id: string) => requestV1<TrustScore>(`/registry/tools/${id}/trust`),
    pricing: (id: string) => requestV1<Record<string, unknown>>(`/registry/tools/${id}/pricing`),
    changeRequests: (params: { status?: string } = {}) =>
      listOf<ChangeRequest>(`/registry/change-requests${query(params)}`, 'change_requests'),
    decide: (id: string, decision: string, reason?: string) =>
      post<ChangeRequest>(`/registry/change-requests/${id}/decide`, { decision, reason }),
  },

  discovery: {
    capabilities: () => requestV1<Capabilities>('/discovery/capabilities'),
    search: (body: { intent: string; limit?: number; requirements?: Record<string, unknown> }) =>
      post<DiscoveryResult>('/discovery/search', body),
    evaluate: (body: { intent: string; tool_id?: string }) =>
      post<Record<string, unknown>>('/discovery/evaluate', body),
    invalidateCache: () => post<Record<string, unknown>>('/discovery/cache/invalidate'),
  },

  sources: {
    connectors: () => listOf<Record<string, unknown>>('/sources/connectors', 'connectors'),
    list: () => listOf<ApiSource>('/sources', 'sources'),
    get: (id: string) => requestV1<ApiSource>(`/sources/${id}`),
    check: (id: string) => post<Record<string, unknown>>(`/sources/${id}/check`),
    drift: (id: string) => listOf<DriftReport>(`/sources/${id}/drift`, 'reports'),
    applyDrift: (reportId: string) =>
      post<Record<string, unknown>>(`/sources/drift/${reportId}/apply`),
    dismissDrift: (reportId: string) =>
      post<Record<string, unknown>>(`/sources/drift/${reportId}/dismiss`),
  },

  documentation: {
    capabilities: () => requestV1<Capabilities>('/documentation/capabilities'),
    documents: () => listOf<DocumentRecord>('/documentation/documents', 'documents'),
    document: (id: string) => requestV1<DocumentRecord>(`/documentation/documents/${id}`),
    chunks: (id: string) =>
      listOf<Record<string, unknown>>(`/documentation/documents/${id}/chunks`, 'chunks'),
    reprocess: (id: string) =>
      post<Record<string, unknown>>(`/documentation/documents/${id}/reprocess`),
    rules: () => listOf<Record<string, unknown>>('/documentation/rules', 'rules'),
    workflows: () => listOf<Record<string, unknown>>('/documentation/workflows', 'workflows'),
    glossary: () => listOf<Record<string, unknown>>('/documentation/glossary', 'terms'),
    graph: () => requestV1<Record<string, unknown>>('/documentation/graph'),
    search: (q: string) =>
      requestV1<Record<string, unknown>>(`/documentation/search${query({ q })}`),
  },

  governance: {
    capabilities: () => requestV1<Capabilities>('/governance/capabilities'),
    policies: () => listOf<GovernancePolicy>('/governance/policies', 'policies'),
    policy: (id: string) => requestV1<GovernancePolicy>(`/governance/policies/${id}`),
    evaluate: (body: Record<string, unknown>) =>
      post<Record<string, unknown>>('/governance/evaluate', body),
    transition: (policyId: string, version: number, state: string, reason?: string) =>
      post<Record<string, unknown>>(
        `/governance/policies/${policyId}/versions/${version}/transition`,
        { state, reason },
      ),
    rollback: (policyId: string) =>
      post<Record<string, unknown>>(`/governance/policies/${policyId}/rollback`),
    complianceRuns: () => listOf<ComplianceRun>('/governance/compliance/runs', 'runs'),
    complianceRun: (id: string) => requestV1<ComplianceRun>(`/governance/compliance/runs/${id}`),
    startComplianceRun: (body: Record<string, unknown>) =>
      post<ComplianceRun>('/governance/compliance/runs', body),
    risk: (toolId: string) => requestV1<Record<string, unknown>>(`/governance/risk/${toolId}`),
    reviews: () => listOf<GovernanceReview>('/governance/reviews', 'reviews'),
    review: (id: string) => requestV1<GovernanceReview>(`/governance/reviews/${id}`),
    decideReview: (id: string, decision: string, reason?: string) =>
      post<GovernanceReview>(`/governance/reviews/${id}/decide`, { decision, reason }),
    refreshReview: (id: string) => post<GovernanceReview>(`/governance/reviews/${id}/refresh`),
    exceptions: () => listOf<GovernanceException>('/governance/exceptions', 'exceptions'),
    decideException: (id: string, decision: string, reason?: string) =>
      post<GovernanceException>(`/governance/exceptions/${id}/decide`, { decision, reason }),
    revokeException: (id: string, reason?: string) =>
      post<GovernanceException>(`/governance/exceptions/${id}/revoke`, { reason }),
  },

  provisioning: {
    capabilities: () => requestV1<Capabilities>('/provisioning/capabilities'),
    whoami: () => requestV1<Record<string, unknown>>('/provisioning/whoami'),
    agents: () => listOf<AgentIdentity>('/provisioning/agents', 'agents'),
    agent: (id: string) => requestV1<AgentIdentity>(`/provisioning/agents/${id}`),
    createAgent: (body: Record<string, unknown>) =>
      post<AgentIdentity>('/provisioning/agents', body),
    revokeAgent: (id: string, reason?: string) =>
      post<AgentIdentity>(`/provisioning/agents/${id}/revoke`, { reason }),
    passes: () => listOf<AccessPass>('/provisioning/passes', 'passes'),
    pass: (id: string) => requestV1<AccessPass>(`/provisioning/passes/${id}`),
    revokePass: (id: string, reason?: string) =>
      post<AccessPass>(`/provisioning/passes/${id}/revoke`, { reason }),
    rules: () => listOf<Record<string, unknown>>('/provisioning/rules', 'rules'),
    decide: (body: Record<string, unknown>) =>
      post<Record<string, unknown>>('/provisioning/decisions', body),
  },

  metering: {
    capabilities: () => requestV1<Capabilities>('/metering/capabilities'),
  },

  billing: {
    plans: () => listOf<PricingPlan>('/billing/plans', 'plans'),
    createPlan: (body: Record<string, unknown>) => post<PricingPlan>('/billing/plans', body),
    publishPlan: (id: string) => post<PricingPlan>(`/billing/plans/${id}/publish`),
    invoices: () => listOf<Invoice>('/billing/invoices', 'invoices'),
    invoice: (id: string) => requestV1<Invoice>(`/billing/invoices/${id}`),
    generateInvoice: (body: { period_start: string; period_end: string }) =>
      post<Invoice>('/billing/invoices', body),
    issueInvoice: (id: string) => post<Invoice>(`/billing/invoices/${id}/issue`),
    voidInvoice: (id: string, reason: string) =>
      post<Invoice>(`/billing/invoices/${id}/void`, { reason }),
    // The ledger answers with the entries *and* the two account balances,
    // which is more than a list — the page shows both.
    ledger: () =>
      requestV1<{
        entries: LedgerEntry[]
        receivable?: { balance_micros?: number; currency?: string }
        payable?: { balance_micros?: number; currency?: string }
      }>('/billing/ledger'),
  },

  settlements: {
    list: () => listOf<Settlement>('/settlements', 'settlements'),
    run: (body: Record<string, unknown>) => post<Settlement>('/settlements/run', body),
    retry: (id: string) => post<Settlement>(`/settlements/${id}/retry`),
  },

  observability: {
    capabilities: () => requestV1<Capabilities>('/observability/capabilities'),
    summary: (windowHours: number) =>
      requestV1<TelemetrySummary>(`/observability/summary${query({ window_hours: windowHours })}`),
    operations: (windowHours: number, limit = 50) =>
      requestV1<{ operations: TelemetryOperation[]; count: number }>(
        `/observability/operations${query({ window_hours: windowHours, limit })}`,
      ),
    operation: (correlationId: string) =>
      requestV1<TelemetryTimeline>(
        `/observability/operations/${encodeURIComponent(correlationId)}`,
      ),
  },

  platform: {
    capabilities: () =>
      requestV1<{ capabilities: PlatformCapability[]; degraded: string[] }>(
        '/platform/capabilities',
      ),
    services: () =>
      requestV1<{
        planes: Record<string, Array<{ name: string; description: string; tables: string[] }>>
        service_count: number
      }>('/platform/services'),
    mode: () => requestV1<PlatformMode>('/platform/mode'),
    leadership: () => requestV1<Leadership>('/platform/leadership'),
    scaling: () => requestV1<ScalingReport>('/platform/scaling'),
  },

  resilience: {
    policy: () => requestV1<ResiliencePolicy>('/resilience/policy'),
    providers: () =>
      requestV1<{ providers: ProviderHealth[]; quarantined: string[]; scope: string }>(
        '/resilience/providers',
      ),
    closeCircuit: (provider: string) =>
      post<ProviderHealth>(`/resilience/providers/${encodeURIComponent(provider)}/close`),
  },

  events: {
    list: (params: { correlation_id?: string; event_type?: string; limit?: number } = {}) =>
      listOf<OutboxEventRow>(`/events${query(params)}`, 'events'),
    deadLetter: () => listOf<OutboxEventRow>('/events/dead-letter', 'events'),
    retry: (eventId: string) => post<Record<string, unknown>>(`/events/${eventId}/retry`),
  },

  generation: {
    capabilities: () => requestV1<Capabilities>('/generation/capabilities'),
    runtimes: () => listOf<RuntimeArtifact>('/generation/runtimes', 'artifacts'),
    runtime: (id: string) => requestV1<RuntimeArtifact>(`/generation/runtimes/${id}`),
    manifest: (id: string) =>
      requestV1<Record<string, unknown>>(`/generation/runtimes/${id}/manifest`),
    sbom: (id: string) => requestV1<Record<string, unknown>>(`/generation/runtimes/${id}/sbom`),
    validation: (id: string) =>
      requestV1<Record<string, unknown>>(`/generation/runtimes/${id}/validation`),
  },
}

// ── Formatting helpers shared by the /v1 pages ───────────────────────────────

/** Micro-units are the platform's money representation: 1 000 000 = one unit
 *  of currency. Never do this with a float — see ADR-064. */
export function formatMicros(micros: number | null | undefined, currency = 'USD'): string {
  if (micros === null || micros === undefined) return '—'
  const whole = Math.trunc(Math.abs(micros) / 1_000_000)
  const fraction = Math.abs(micros) % 1_000_000
  const cents = String(Math.round(fraction / 10_000)).padStart(2, '0')
  const sign = micros < 0 ? '-' : ''
  return `${sign}${whole.toLocaleString()}.${cents} ${currency}`
}

export function formatBps(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return '—'
  return `${(bps / 100).toFixed(bps % 100 === 0 ? 0 : 2)}%`
}

export function formatWhen(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value.endsWith('Z') || value.includes('+') ? value : `${value}Z`)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}
