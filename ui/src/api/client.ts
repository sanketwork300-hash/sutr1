const TOKEN_KEY = 'sutr_token'
// Pre-rebrand (AgentPort) storage key — migrated on first load so existing
// sessions survive the upgrade.
const LEGACY_TOKEN_KEY = 'agent_port_token'
{
  const legacy = localStorage.getItem(LEGACY_TOKEN_KEY)
  if (legacy && !localStorage.getItem(TOKEN_KEY)) {
    localStorage.setItem(TOKEN_KEY, legacy)
  }
  localStorage.removeItem(LEGACY_TOKEN_KEY)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function getErrorMessage(body: unknown, status: number): string {
  if (isRecord(body)) {
    if (typeof body.detail === 'string') return body.detail
    if (isRecord(body.detail) && typeof body.detail.message === 'string') {
      return body.detail.message
    }
    if (typeof body.message === 'string') return body.message
  }
  return `Request failed: ${status}`
}

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, body: unknown, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const headers = new Headers(init?.headers)
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  if (!headers.has('Content-Type') && !(init?.body instanceof URLSearchParams)) {
    headers.set('Content-Type', 'application/json')
  }

  const res = await fetch(`/api${path}`, { ...init, headers })

  if (res.status === 401) {
    clearToken()
    window.location.href = '/login'
    throw new Error('Unauthorized')
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new ApiError(res.status, body, getErrorMessage(body, res.status))
  }

  if (res.status === 204) return undefined as T
  return res.json()
}

async function requestBlob(
  path: string,
  init?: RequestInit,
): Promise<{ blob: Blob; filename: string | null }> {
  const token = getToken()
  const headers = new Headers(init?.headers)
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  if (!headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const res = await fetch(`/api${path}`, { ...init, headers })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new ApiError(res.status, body, getErrorMessage(body, res.status))
  }
  const disposition = res.headers.get('content-disposition') ?? ''
  const match = disposition.match(/filename="([^"]+)"/)
  return { blob: await res.blob(), filename: match ? match[1] : null }
}

// ── Types ──

export interface AuthMethod {
  method: 'oauth' | 'token' | 'env_var'
  label?: string
  header?: string
  format?: string
  note?: string
  registration_url?: string
  acquire_url?: string
  env?: string
}

export interface BundledIntegration {
  id: string
  name: string
  type: string
  description: string
  url?: string
  docs_url?: string
  auth: AuthMethod[]
  available: boolean
  available_reason: string | null
  tools?: Array<{ name: string; description?: string }>
  tool_categories?: Record<string, string>
}

export interface CustomMcpIntegration {
  id: string
  integration_id: string
  name: string
  url: string
  description: string | null
  auth_method: 'none' | 'token' | 'oauth'
  token_header: string
  token_format: string
  created_at: string
}

export interface ApiParam {
  name: string
  type?: string
  description?: string | null
  required?: boolean
  default?: unknown
  enum?: string[] | null
  items?: string | null
  query?: boolean
  schema_override?: Record<string, unknown> | null
}

export interface ApiToolDefinition {
  name: string
  description: string
  method: string
  path: string
  params?: ApiParam[]
}

export interface CustomApiIntegration {
  id: string
  integration_id: string
  name: string
  description: string | null
  base_url: string
  token_header: string
  token_format: string
  tools: ApiToolDefinition[]
  created_at: string
  updated_at: string
}

export interface CreateCustomApiRequest {
  name: string
  description?: string | null
  base_url: string
  token_header?: string
  token_format?: string
  tools?: ApiToolDefinition[]
}

export interface UpdateCustomApiRequest {
  name?: string
  description?: string | null
  base_url?: string
  token_header?: string
  token_format?: string
  tools?: ApiToolDefinition[]
}

export interface TestCustomApiRequest {
  base_url: string
  token_header: string
  token_format: string
  token: string
  integration_db_id?: string
  tool: ApiToolDefinition
  args: Record<string, unknown>
}

export interface CustomApiTestResult {
  content: { type: string; text: string }[]
  isError: boolean
  status_code?: number | null
  duration_ms?: number
}

export interface OpenApiWarning {
  code: string
  message: string
  context?: string | null
}

export interface OpenApiOperationSummary {
  operation_id: string | null
  method: string
  path: string
  summary: string | null
  tags: string[]
  deprecated: boolean
}

export interface OpenApiServer {
  url: string
  description?: string | null
  variables?: Record<string, { default?: string; enum?: string[] }>
}

export interface OpenApiProject {
  id: string
  name: string
  source_kind: string
  source_url: string | null
  status: string
  api_title: string
  api_version: string
  openapi_version: string
  operation_count: number
  integration_db_id: string | null
  created_at: string
  updated_at: string
  warnings: OpenApiWarning[]
  // Detail-only fields:
  description?: string | null
  servers?: OpenApiServer[]
  suggested_auth?: { token_header: string; token_format: string; warnings: OpenApiWarning[] }
  operations?: OpenApiOperationSummary[]
  tags?: string[]
  server_url?: string | null
  server_variables?: Record<string, string>
}

export interface OpenApiImportRequest {
  name?: string
  source_kind: 'paste' | 'upload' | 'url'
  content?: string
  url?: string
}

export interface OpenApiCompileRequest {
  filters?: {
    include_tags?: string[]
    exclude_tags?: string[]
    include_paths?: string[]
    exclude_paths?: string[]
    include_deprecated?: boolean
  }
  server_url?: string
  server_variables?: Record<string, string>
  auth?: { token_header: string; token_format: string }
  integration_name?: string
  dry_run?: boolean
}

export interface OpenApiCompiledToolPreview {
  name: string
  description: string
  method: string
  path: string
  tags: string[]
  operation_id: string | null
  renamed_from: string | null
  param_count: number
}

export interface OpenApiCompileResult {
  tools: OpenApiCompiledToolPreview[]
  warnings: OpenApiWarning[]
  auth: { token_header: string; token_format: string }
  base_url: string
  dry_run: boolean
  integration_db_id?: string
  integration_id?: string
  project?: OpenApiProject
}

export interface Deployment {
  id: string
  name: string
  slug: string
  provider: string
  status: string
  url: string | null
  health_url?: string | null
  error: string | null
  tool_count: number
  project_id: string | null
  env_var: string | null
  has_token: boolean
  created_at: string
  updated_at: string
}

export interface AuditEvent {
  id: number
  org_id: string
  timestamp: string
  action: string
  actor_type: string
  actor_user_id: string | null
  actor_api_key_prefix: string | null
  impersonator_user_id: string | null
  target_type: string | null
  target_id: string | null
  summary: string
  metadata_json: string
  ip: string | null
  user_agent: string | null
}

export interface UsageSummary {
  start: string
  end: string
  tool_calls: number
  totals_by_kind: { kind: string; quantity: number; events: number }[]
  tool_calls_by_outcome: { outcome: string | null; count: number }[]
  tool_calls_by_source: { source: string | null; count: number }[]
  top_integrations: { integration_id: string | null; count: number }[]
  top_tools: { integration_id: string | null; tool_name: string | null; count: number }[]
  daily: { date: string; count: number }[]
  duration_ms: { avg: number | null; max: number | null }
}

export interface DeploymentProviderInfo {
  id: string
  display_name: string
  enabled: boolean
  reason: string | null
}

export interface CreateDeploymentRequest {
  project_id: string
  name: string
  provider: string
  token?: string | null
  compile?: OpenApiCompileRequest
}

export interface CreateCustomMcpRequest {
  name: string
  url: string
  description?: string
  auth_method: 'none' | 'token' | 'oauth'
  token_header?: string
  token_format?: string
}

export interface InstalledIntegration {
  id: string
  org_id: string
  integration_id: string
  type: string
  url: string
  auth_method: string
  connected: boolean
  added_at: string
}

export interface ToolAnnotations {
  title?: string
  readOnlyHint?: boolean
  destructiveHint?: boolean
  idempotentHint?: boolean
  openWorldHint?: boolean
}

export interface ToolIcon {
  src: string
  mimeType?: string
  sizes?: string[]
}

export interface Tool {
  name: string
  title?: string
  description?: string
  inputSchema?: Record<string, unknown>
  outputSchema?: Record<string, unknown>
  icons?: ToolIcon[]
  annotations?: ToolAnnotations
  execution?: Record<string, unknown>
  meta?: Record<string, unknown>
  integration_id?: string
  category?: string | null
  execution_mode?: string
}

export interface ToolCallSuccess {
  content: { type: string; text: string }[]
  isError: boolean
}

export interface ToolCallApprovalRequired {
  error: 'approval_required'
  approval_request_id: string
  approval_url: string
  message: string
}

export interface ToolCallDenied {
  error: 'denied'
  message: string
}

export interface LogEntry {
  id: number
  org_id: string
  timestamp: string
  integration_id: string
  tool_name: string
  args_json?: string
  result_json?: string
  error?: string
  duration_ms?: number
  outcome?: string
  approval_request_id?: string | null
  args_hash?: string | null
  requester_ip?: string | null
  user_agent?: string | null
  api_key_label?: string | null
  api_key_prefix?: string | null
  access_reason?: string | null // approved_once | approved_exact | approved_any | null
  approval_expires_at?: string | null
  additional_info?: string | null
}

export interface ApprovalRequest {
  id: string
  org_id: string
  integration_id: string
  tool_name: string
  args_json: string
  args_hash: string
  summary_text: string
  status: string
  requested_by_agent: string | null
  requested_at: string
  expires_at: string
  decision_mode: string | null
  decided_by_user_id: string | null
  decided_at: string | null
  consumed_at: string | null
  requester_ip: string | null
  user_agent: string | null
  api_key_label: string | null
  api_key_prefix: string | null
  approver_ip: string | null
  additional_info: string | null
}

export interface PublicConfig {
  is_self_hosted: boolean
  billing_enabled: boolean
}

export interface ToolExecutionSettingResponse {
  id: string
  org_id: string
  integration_id: string
  tool_name: string
  mode: string
  updated_by_user_id: string | null
  updated_at: string
}

export interface ApiKey {
  id: string
  name: string
  key_prefix: string
  created_at: string
  last_used_at: string | null
  is_active: boolean
}

export interface CreateApiKeyResponse {
  id: string
  name: string
  key_prefix: string
  created_at: string
  plain_key: string
}

export interface RegisterResponse {
  user_id: string
  org_id: string
  email: string
  email_verification_required: boolean
  verification_token: string | null
  resend_available_at: string | null
}

export interface TotpStatusResponse {
  enabled: boolean
  configured: boolean
}

export interface TotpSetupResponse {
  secret: string
  otpauth_uri: string
  qr_data_url: string
  recovery_codes: string[]
}

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface SubscriptionResponse {
  tier: 'free' | 'plus'
  status: string
  current_period_end: string | null
  cancel_at_period_end: boolean
  enterprise_contact_email: string
}

export interface BillingRedirectResponse {
  url: string
}

export interface OrgSettingsResponse {
  approval_expiry_minutes: number
  approval_expiry_minutes_default: number
  approval_expiry_minutes_override: number | null
}

export interface OrgInfo {
  id: string
  name: string
  role: string
}

export interface OrgMember {
  user_id: string
  email: string
  role: string
  is_you: boolean
}

export interface OrgInvitation {
  id: string
  email: string
  role: string
  created_at: string
  expires_at: string
  status: 'pending' | 'accepted' | 'revoked' | 'expired'
}

export interface CreateInvitationResponse extends OrgInvitation {
  invite_url: string
}

export interface InvitationPreview {
  org_name: string
  email: string
  role: string
  account_exists: boolean
}

export interface AcceptInvitationResponse {
  org_id: string
  org_name: string
  role: string
  created_account: boolean
  access_token: string | null
}

export interface Workspace {
  id: string
  name: string
  slug: string
  is_default: boolean
}

// ── API ──

export interface MessageResponse {
  message: string
}

export interface VerifyEmailResponse extends MessageResponse {
  email: string
}

export interface VerifyEmailCodeResponse extends MessageResponse {
  access_token: string | null
  token_type: string | null
}

export interface ResendVerificationResponse extends MessageResponse {
  resend_available_at: string | null
}

export interface EmailVerificationRequiredDetail {
  error: 'email_verification_required'
  message: string
  email: string
  verification_token: string
  resend_available_at: string | null
}

export interface VerificationEmailRateLimitedDetail {
  error: 'verification_email_rate_limited'
  message: string
  resend_available_at: string
}

export const api = {
  auth: {
    register(email: string, password: string) {
      return request<RegisterResponse>('/users/register', {
        method: 'POST',
        body: JSON.stringify({ email, password }),
      })
    },
    login(email: string, password: string, totpCode?: string) {
      const body = new URLSearchParams({ username: email, password })
      if (totpCode) body.set('totp_code', totpCode)
      return request<TokenResponse>('/auth/token', {
        method: 'POST',
        body,
      })
    },
    logout() {
      return request<void>('/auth/logout', { method: 'POST' })
    },
    verifyEmail(token: string) {
      return request<VerifyEmailResponse>(`/auth/verify-email?token=${encodeURIComponent(token)}`)
    },
    verifyEmailCode(code: string, verification_token: string) {
      return request<VerifyEmailCodeResponse>('/auth/verify-email-code', {
        method: 'POST',
        body: JSON.stringify({ code, verification_token }),
      })
    },
    resendVerification() {
      return request<ResendVerificationResponse>('/auth/resend-verification', { method: 'POST' })
    },
    resendVerificationCode(verification_token: string) {
      return request<ResendVerificationResponse>('/auth/resend-verification-code', {
        method: 'POST',
        body: JSON.stringify({ verification_token }),
      })
    },
    forgotPassword(email: string) {
      return request<MessageResponse>('/auth/forgot-password', {
        method: 'POST',
        body: JSON.stringify({ email }),
      })
    },
    resetPassword(token: string, new_password: string) {
      return request<MessageResponse>('/auth/reset-password', {
        method: 'POST',
        body: JSON.stringify({ token, new_password }),
      })
    },
    changePassword(current_password: string, new_password: string) {
      return request<MessageResponse>('/users/me/change-password', {
        method: 'POST',
        body: JSON.stringify({ current_password, new_password }),
      })
    },
    me() {
      return request<{
        id: string
        email: string
        totp_enabled: boolean
        totp_configured: boolean
        is_admin: boolean
        impersonator_email: string | null
      }>('/users/me')
    },
    googleLogin() {
      return request<{ authorization_url: string }>('/auth/google/login')
    },
  },
  totp: {
    status() {
      return request<TotpStatusResponse>('/users/me/totp/status')
    },
    setup() {
      return request<TotpSetupResponse>('/users/me/totp/setup', { method: 'POST' })
    },
    enable(code: string) {
      return request<TotpStatusResponse>('/users/me/totp/enable', {
        method: 'POST',
        body: JSON.stringify({ code }),
      })
    },
    reEnable(code: string) {
      return request<TotpStatusResponse>('/users/me/totp/re-enable', {
        method: 'POST',
        body: JSON.stringify({ code }),
      })
    },
    disable(code: string) {
      return request<MessageResponse>('/users/me/totp/disable', {
        method: 'POST',
        body: JSON.stringify({ code }),
      })
    },
  },
  integrations: {
    list() {
      return request<BundledIntegration[]>('/integrations')
    },
  },
  customMcp: {
    list() {
      return request<CustomMcpIntegration[]>('/integrations/custom')
    },
    create(data: CreateCustomMcpRequest) {
      return request<CustomMcpIntegration>('/integrations/custom', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
    remove(id: string) {
      return request<void>(`/integrations/custom/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
  },
  customApi: {
    list() {
      return request<CustomApiIntegration[]>('/integrations/custom-api')
    },
    create(data: CreateCustomApiRequest) {
      return request<CustomApiIntegration>('/integrations/custom-api', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
    get(id: string) {
      return request<CustomApiIntegration>(`/integrations/custom-api/${encodeURIComponent(id)}`)
    },
    update(id: string, data: UpdateCustomApiRequest) {
      return request<CustomApiIntegration>(`/integrations/custom-api/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify(data),
      })
    },
    remove(id: string) {
      return request<void>(`/integrations/custom-api/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      })
    },
    test(data: TestCustomApiRequest) {
      return request<CustomApiTestResult>('/integrations/custom-api/test', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
  },
  openapi: {
    list() {
      return request<OpenApiProject[]>('/openapi')
    },
    import(data: OpenApiImportRequest) {
      return request<OpenApiProject>('/openapi/import', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
    get(id: string) {
      return request<OpenApiProject>(`/openapi/${encodeURIComponent(id)}`)
    },
    compile(id: string, data: OpenApiCompileRequest) {
      return request<OpenApiCompileResult>(`/openapi/${encodeURIComponent(id)}/compile`, {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
    downloadPackage(id: string, data: OpenApiCompileRequest) {
      return requestBlob(`/openapi/${encodeURIComponent(id)}/package`, {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
    remove(id: string) {
      return request<void>(`/openapi/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
  },
  usage: {
    summary(params?: { start?: string; end?: string }) {
      const qs = new URLSearchParams()
      if (params?.start) qs.set('start', params.start)
      if (params?.end) qs.set('end', params.end)
      const suffix = qs.toString() ? `?${qs}` : ''
      return request<UsageSummary>(`/usage/summary${suffix}`)
    },
  },
  deployments: {
    providers() {
      return request<DeploymentProviderInfo[]>('/deployments/providers')
    },
    list() {
      return request<Deployment[]>('/deployments')
    },
    create(data: CreateDeploymentRequest) {
      return request<Deployment>('/deployments', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
    get(id: string) {
      return request<Deployment>(`/deployments/${encodeURIComponent(id)}`)
    },
    logs(id: string, tail = 200) {
      return request<{ logs: string }>(`/deployments/${encodeURIComponent(id)}/logs?tail=${tail}`)
    },
    stop(id: string) {
      return request<Deployment>(`/deployments/${encodeURIComponent(id)}/stop`, {
        method: 'POST',
      })
    },
    start(id: string) {
      return request<Deployment>(`/deployments/${encodeURIComponent(id)}/start`, {
        method: 'POST',
      })
    },
    remove(id: string) {
      return request<void>(`/deployments/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
  },
  installed: {
    list() {
      return request<InstalledIntegration[]>('/installed')
    },
    create(data: { integration_id: string; auth_method: string; token?: string }) {
      return request<InstalledIntegration>('/installed', {
        method: 'POST',
        body: JSON.stringify(data),
      })
    },
    update(integration_id: string, token: string) {
      return request<InstalledIntegration>(`/installed/${encodeURIComponent(integration_id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ token }),
      })
    },
    remove(integration_id: string) {
      return request<void>(`/installed/${encodeURIComponent(integration_id)}`, { method: 'DELETE' })
    },
  },
  tools: {
    listAll() {
      return request<Tool[]>('/tools')
    },
    listForIntegration(integration_id: string) {
      return request<Tool[]>(`/tools/${encodeURIComponent(integration_id)}`)
    },
    async call(
      integrationId: string,
      toolName: string,
      args: Record<string, unknown>,
      additionalInfo?: string,
    ): Promise<
      | { status: 'ok'; data: ToolCallSuccess }
      | { status: 'approval_required'; data: ToolCallApprovalRequired }
      | { status: 'denied'; data: ToolCallDenied }
      | { status: 'error'; message: string }
    > {
      const token = getToken()
      const headers = new Headers({ 'Content-Type': 'application/json' })
      if (token) headers.set('Authorization', `Bearer ${token}`)
      const payload: Record<string, unknown> = { tool_name: toolName, args }
      if (additionalInfo) payload.additional_info = additionalInfo
      const res = await fetch(`/api/tools/${encodeURIComponent(integrationId)}/call`, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
      })
      const body = await res.json().catch(() => ({}))
      if (res.status === 401) {
        clearToken()
        window.location.href = '/login'
        return { status: 'error', message: 'Unauthorized' }
      }
      if (res.ok) return { status: 'ok', data: body as ToolCallSuccess }
      if (res.status === 403) {
        if (body.error === 'approval_required')
          return { status: 'approval_required', data: body as ToolCallApprovalRequired }
        if (body.error === 'denied') return { status: 'denied', data: body as ToolCallDenied }
      }
      return { status: 'error', message: body.detail || `Request failed: ${res.status}` }
    },
  },
  logs: {
    list(params?: {
      integration?: string
      tool?: string
      outcome?: string
      limit?: number
      offset?: number
    }) {
      const q = new URLSearchParams()
      if (params?.integration) q.set('integration', params.integration)
      if (params?.tool) q.set('tool', params.tool)
      if (params?.outcome) q.set('outcome', params.outcome)
      if (params?.limit) q.set('limit', String(params.limit))
      if (params?.offset) q.set('offset', String(params.offset))
      const qs = q.toString()
      return request<LogEntry[]>(`/logs${qs ? `?${qs}` : ''}`)
    },
  },
  oauth: {
    start(integration_id: string) {
      return request<{ authorization_url: string; state: string }>(
        `/auth/${encodeURIComponent(integration_id)}/start`,
        { method: 'POST' },
      )
    },
  },
  audit: {
    list(params?: { action?: string; limit?: number; offset?: number }) {
      const qs = new URLSearchParams()
      if (params?.action) qs.set('action', params.action)
      qs.set('limit', String(params?.limit ?? 100))
      qs.set('offset', String(params?.offset ?? 0))
      return request<AuditEvent[]>(`/audit?${qs}`)
    },
  },
  approvals: {
    list(params?: { status?: string; limit?: number; offset?: number }) {
      const qs = new URLSearchParams()
      if (params?.status) qs.set('status', params.status)
      qs.set('limit', String(params?.limit ?? 100))
      qs.set('offset', String(params?.offset ?? 0))
      return request<ApprovalRequest[]>(`/tool-approvals/requests?${qs}`)
    },
    get(id: string) {
      return request<ApprovalRequest>(`/tool-approvals/requests/${id}`)
    },
    approveOnce(id: string, totp_code?: string) {
      return request<ApprovalRequest>(`/tool-approvals/requests/${id}/approve-once`, {
        method: 'POST',
        body: JSON.stringify({ totp_code: totp_code ?? null }),
      })
    },
    approveExact(id: string, totp_code?: string) {
      return request<ApprovalRequest>(`/tool-approvals/requests/${id}/approve-exact`, {
        method: 'POST',
        body: JSON.stringify({ totp_code }),
      })
    },
    allowTool(id: string, totp_code?: string) {
      return request<ApprovalRequest>(`/tool-approvals/requests/${id}/allow-tool`, {
        method: 'POST',
        body: JSON.stringify({ totp_code: totp_code ?? null }),
      })
    },
    deny(id: string, totp_code?: string) {
      return request<ApprovalRequest>(`/tool-approvals/requests/${id}/deny`, {
        method: 'POST',
        body: JSON.stringify({ totp_code: totp_code ?? null }),
      })
    },
  },
  apiKeys: {
    list() {
      return request<ApiKey[]>('/api-keys')
    },
    create(name: string) {
      return request<CreateApiKeyResponse>('/api-keys', {
        method: 'POST',
        body: JSON.stringify({ name }),
      })
    },
    revoke(id: string) {
      return request<void>(`/api-keys/${id}`, { method: 'DELETE' })
    },
  },
  oauthConsent: {
    getSession(sessionToken: string) {
      return request<{
        client_id: string
        client_name: string | null
        redirect_uri: string
        scope: string | null
        resource: string | null
        expires_at: number
      }>(`/oauth/authorize/session?session=${encodeURIComponent(sessionToken)}`)
    },
    approve(sessionToken: string) {
      return request<{ redirect_url: string }>('/oauth/authorize/approve', {
        method: 'POST',
        body: JSON.stringify({ session_token: sessionToken }),
      })
    },
    deny(sessionToken: string) {
      return request<{ redirect_url: string }>('/oauth/authorize/deny', {
        method: 'POST',
        body: JSON.stringify({ session_token: sessionToken }),
      })
    },
  },
  toolSettings: {
    list(integration_id: string) {
      return request<ToolExecutionSettingResponse[]>(
        `/tool-settings/${encodeURIComponent(integration_id)}`,
      )
    },
    update(integration_id: string, toolName: string, mode: string, totp_code?: string) {
      return request<ToolExecutionSettingResponse>(
        `/tool-settings/${encodeURIComponent(integration_id)}/${encodeURIComponent(toolName)}`,
        {
          method: 'PUT',
          body: JSON.stringify({ mode, totp_code: totp_code ?? null }),
        },
      )
    },
  },
  config: {
    get() {
      return request<PublicConfig>('/config')
    },
  },
  orgSettings: {
    get() {
      return request<OrgSettingsResponse>('/org-settings')
    },
    update(approval_expiry_minutes: number | null) {
      return request<OrgSettingsResponse>('/org-settings', {
        method: 'PATCH',
        body: JSON.stringify({ approval_expiry_minutes }),
      })
    },
  },
  org: {
    get() {
      return request<OrgInfo>('/org')
    },
    rename(name: string) {
      return request<OrgInfo>('/org', { method: 'PATCH', body: JSON.stringify({ name }) })
    },
    listMembers() {
      return request<OrgMember[]>('/org/members')
    },
    updateMemberRole(userId: string, role: string) {
      return request<OrgMember>(`/org/members/${encodeURIComponent(userId)}`, {
        method: 'PATCH',
        body: JSON.stringify({ role }),
      })
    },
    removeMember(userId: string) {
      return request<void>(`/org/members/${encodeURIComponent(userId)}`, { method: 'DELETE' })
    },
    listInvitations() {
      return request<OrgInvitation[]>('/org/invitations')
    },
    createInvitation(email: string, role: string) {
      return request<CreateInvitationResponse>('/org/invitations', {
        method: 'POST',
        body: JSON.stringify({ email, role }),
      })
    },
    revokeInvitation(id: string) {
      return request<void>(`/org/invitations/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
  },
  orgInvitations: {
    preview(token: string) {
      return request<InvitationPreview>(
        `/org-invitations/preview?token=${encodeURIComponent(token)}`,
      )
    },
    accept(token: string, password?: string) {
      return request<AcceptInvitationResponse>('/org-invitations/accept', {
        method: 'POST',
        body: JSON.stringify({ token, password: password ?? null }),
      })
    },
  },
  workspaces: {
    list() {
      return request<Workspace[]>('/workspaces')
    },
    create(name: string) {
      return request<Workspace>('/workspaces', { method: 'POST', body: JSON.stringify({ name }) })
    },
    rename(id: string, name: string) {
      return request<Workspace>(`/workspaces/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ name }),
      })
    },
    remove(id: string) {
      return request<void>(`/workspaces/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
  },
  billing: {
    getSubscription() {
      return request<SubscriptionResponse>('/billing/subscription')
    },
    createCheckout() {
      return request<BillingRedirectResponse>('/billing/checkout', { method: 'POST' })
    },
    openPortal() {
      return request<BillingRedirectResponse>('/billing/portal', { method: 'POST' })
    },
  },
  admin: {
    getSettings() {
      return request<{ waitlist_enabled: boolean }>('/admin/settings')
    },
    updateSettings(waitlist_enabled: boolean) {
      return request<{ waitlist_enabled: boolean }>('/admin/settings', {
        method: 'PATCH',
        body: JSON.stringify({ waitlist_enabled }),
      })
    },
    listWaitlist() {
      return request<Array<{ id: string; email: string; added_at: string }>>('/admin/waitlist')
    },
    addWaitlistEmail(email: string) {
      return request<{ id: string; email: string; added_at: string }>('/admin/waitlist', {
        method: 'POST',
        body: JSON.stringify({ email }),
      })
    },
    removeWaitlistEmail(id: string) {
      return request<void>(`/admin/waitlist/${encodeURIComponent(id)}`, { method: 'DELETE' })
    },
    listUsers(q?: string) {
      const qs = q ? `?q=${encodeURIComponent(q)}` : ''
      return request<
        Array<{
          id: string
          email: string
          is_admin: boolean
          is_active: boolean
          created_at: string
        }>
      >(`/admin/users${qs}`)
    },
    impersonate(userId: string) {
      return request<{ access_token: string; token_type: string }>(
        `/admin/impersonate/${encodeURIComponent(userId)}`,
        { method: 'POST' },
      )
    },
    stopImpersonation() {
      return request<{ access_token: string; token_type: string }>('/admin/impersonate/stop', {
        method: 'POST',
      })
    },
  },
}
