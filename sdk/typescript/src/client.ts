import {
  APIError,
  ApprovalPending,
  ApprovalRequired,
  AuthenticationError,
  NotFoundError,
  PermissionDenied,
  RateLimited,
  ServerError,
  SutrConnectionError,
  SutrError,
  ToolDenied,
} from './errors.js';
import type {
  ApprovalDecision,
  CallToolOptions,
  ContentBlock,
  Integration,
  LogsQuery,
  SutrOptions,
  Tool,
  ToolResult,
  UsageEventsQuery,
  UsageQuery,
} from './types.js';

const DEFAULT_BASE_URL = 'https://app.sutr.sh';
const DEFAULT_TIMEOUT_MS = 30_000;
/** The server holds approval long-polls open for ~240s; allow headroom. */
const APPROVAL_POLL_TIMEOUT_MS = 300_000;
const RETRYABLE = new Set([429, 502, 503, 504]);
/** Floor between approval polls, so a proxy that answers instantly can't turn
 *  the wait into a hot loop. */
const POLL_MIN_INTERVAL_MS = 500;
const USER_AGENT = 'sutr-sdk-js/0.1.0';

interface Prepared {
  method: string;
  path: string;
  body?: unknown;
  query?: Record<string, string | number | undefined>;
  timeoutMs?: number;
  /** False for anything with side effects — a retried tool call could run twice. */
  retryable?: boolean;
}

interface CallContext {
  integrationId?: string;
  toolName?: string;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function detailMessage(body: unknown, status: number): string {
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>;
    const detail = record.detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      const nested = detail as Record<string, unknown>;
      const message = nested.message ?? nested.error;
      if (typeof message === 'string') return message;
    }
    if (Array.isArray(detail)) {
      const parts = detail
        .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
        .map((item) => {
          const loc = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : '';
          return `${loc || 'body'}: ${String(item.msg)}`;
        });
      if (parts.length > 0) return parts.join('; ');
    }
    if (typeof record.message === 'string') return record.message;
  }
  return `HTTP ${status}`;
}

function toTool(row: Record<string, unknown>, fallbackIntegrationId?: string): Tool {
  const integrationId = (row.integration_id as string | undefined) ?? fallbackIntegrationId ?? null;
  return {
    name: (row.name as string) ?? '',
    description: (row.description as string | undefined) ?? null,
    inputSchema: (row.inputSchema as Tool['inputSchema']) ?? {},
    integrationId,
    executionMode: (row.execution_mode as string | undefined) ?? null,
    category: (row.category as string | undefined) ?? null,
    raw: row,
  };
}

function toToolResult(payload: Record<string, unknown>): ToolResult {
  const content = Array.isArray(payload.content) ? (payload.content as ContentBlock[]) : [];
  return {
    content,
    isError: Boolean(payload.isError),
    statusCode: (payload.status_code as number | undefined) ?? null,
    durationMs: (payload.duration_ms as number | undefined) ?? null,
    text: content
      .map((block) => block.text)
      .filter((text): text is string => typeof text === 'string')
      .join('\n'),
    raw: payload,
  };
}

function toApprovalDecision(payload: Record<string, unknown>): ApprovalDecision {
  const status = (payload.status as string) ?? '';
  return {
    approvalRequestId: String(payload.approval_request_id ?? payload.id ?? ''),
    integrationId: (payload.integration_id as string) ?? '',
    toolName: (payload.tool_name as string) ?? '',
    status,
    message: (payload.message as string) ?? '',
    expiresAt: (payload.expires_at as string | undefined) ?? null,
    decisionMode: (payload.decision_mode as string | undefined) ?? null,
    approved: status === 'approved',
    pending: status === 'pending',
    raw: payload,
  };
}

/**
 * Sutr client.
 *
 * ```ts
 * const sutr = new Sutr({ apiKey: 'ap_...', baseUrl: 'https://sutr.example.com' });
 * const result = await sutr.callTool('posthog', 'list_projects', {});
 * console.log(result.text);
 * ```
 */
export class Sutr {
  readonly baseUrl: string;
  private readonly headers: Record<string, string>;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly fetchImpl: typeof globalThis.fetch;

  constructor(options: SutrOptions = {}) {
    const env = typeof process !== 'undefined' ? (process.env ?? {}) : {};
    const accessToken = options.accessToken;
    // Environment credentials are a fallback for when the caller supplied none —
    // never an override. Letting SUTR_API_KEY win over an explicit accessToken
    // would silently send the wrong credential (and a key cannot read
    // user-scoped endpoints such as /api/logs).
    const apiKey =
      options.apiKey ?? (accessToken === undefined ? env.SUTR_API_KEY : undefined);
    if (!apiKey && !accessToken) {
      throw new SutrError(
        'No credentials. Pass apiKey or set SUTR_API_KEY ' +
          '(create a key in the Sutr UI under Settings → API Keys).',
      );
    }

    this.baseUrl = (options.baseUrl ?? env.SUTR_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
    this.headers = { Accept: 'application/json', 'User-Agent': USER_AGENT };
    if (apiKey) this.headers['X-API-Key'] = apiKey;
    else if (accessToken) this.headers.Authorization = `Bearer ${accessToken}`;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxRetries = Math.max(0, options.maxRetries ?? 2);
    const fetchImpl = options.fetch ?? globalThis.fetch;
    if (!fetchImpl) {
      throw new SutrError('No fetch implementation available. Node 18+ or pass options.fetch.');
    }
    this.fetchImpl = fetchImpl;
  }

  // ── Tools ──────────────────────────────────────────────────────────────────

  /** Tools from one installed integration, or all of them. */
  async listTools(integrationId?: string): Promise<Tool[]> {
    const path = integrationId
      ? `/api/tools/${encodeURIComponent(integrationId)}`
      : '/api/tools';
    const payload = await this.send<unknown>({ method: 'GET', path, retryable: true });
    const rows = Array.isArray(payload) ? payload : [];
    return rows
      .filter((row): row is Record<string, unknown> => !!row && typeof row === 'object')
      .map((row) => toTool(row, integrationId));
  }

  /**
   * Execute a tool.
   *
   * Throws `ApprovalRequired` when a human must decide first, or `ToolDenied`
   * when policy blocks the tool. With `waitForApproval: true` the call waits for
   * the decision and then retries the identical request.
   */
  async callTool(
    integrationId: string,
    toolName: string,
    args: Record<string, unknown> = {},
    options: CallToolOptions = {},
  ): Promise<ToolResult> {
    const body: Record<string, unknown> = { tool_name: toolName, args };
    if (options.additionalInfo) body.additional_info = options.additionalInfo;
    const prepared: Prepared = {
      method: 'POST',
      path: `/api/tools/${encodeURIComponent(integrationId)}/call`,
      body,
    };
    const context: CallContext = { integrationId, toolName };

    try {
      return toToolResult((await this.send<Record<string, unknown>>(prepared, context)) ?? {});
    } catch (error) {
      if (!(error instanceof ApprovalRequired) || !options.waitForApproval) throw error;

      const decision = await this.awaitApproval(error.approvalRequestId, {
        timeoutMs: options.approvalTimeoutMs,
      });
      if (!decision.approved) {
        if (decision.pending) {
          throw new ApprovalPending(
            `Still waiting on a human decision: ${error.approvalUrl}`,
            error.approvalUrl,
            error.approvalRequestId,
          );
        }
        throw new SutrError(decision.message || `Approval ${decision.status}`);
      }
      // Approved: the grant is bound to these exact arguments, so resend as-is.
      return toToolResult((await this.send<Record<string, unknown>>(prepared, context)) ?? {});
    }
  }

  /** Block until a human decides, or `timeoutMs` elapses. */
  async awaitApproval(
    approvalRequestId: string,
    options: { timeoutMs?: number } = {},
  ): Promise<ApprovalDecision> {
    const deadline = options.timeoutMs ? Date.now() + options.timeoutMs : null;
    let last: ApprovalDecision | null = null;

    for (;;) {
      let window: number | undefined;
      if (deadline !== null) {
        const remaining = deadline - Date.now();
        if (remaining <= 0) return last ?? (await this.getApproval(approvalRequestId));
        window = Math.max(1, Math.ceil(remaining / 1000));
      }

      const started = Date.now();
      const payload = await this.send<Record<string, unknown>>({
        method: 'POST',
        path: `/api/tool-approvals/requests/${encodeURIComponent(approvalRequestId)}/await`,
        body: window ? { timeout_seconds: window } : undefined,
        timeoutMs: APPROVAL_POLL_TIMEOUT_MS,
        retryable: true,
      });
      const decision = toApprovalDecision(payload ?? {});
      if (!decision.pending) return decision;
      last = decision;
      if (deadline !== null && Date.now() >= deadline) return decision;

      const elapsed = Date.now() - started;
      let delay = POLL_MIN_INTERVAL_MS - elapsed;
      if (deadline !== null) delay = Math.min(delay, Math.max(0, deadline - Date.now()));
      if (delay > 0) await sleep(delay);
    }
  }

  async getApproval(approvalRequestId: string): Promise<ApprovalDecision> {
    const payload = await this.send<Record<string, unknown>>({
      method: 'GET',
      path: `/api/tool-approvals/requests/${encodeURIComponent(approvalRequestId)}`,
      retryable: true,
    });
    return toApprovalDecision({ message: '', ...(payload ?? {}) });
  }

  // ── Integrations ───────────────────────────────────────────────────────────

  /** The catalog, annotated with what this org has installed. */
  async integrations(): Promise<Integration[]> {
    const [catalog, installedRows] = await Promise.all([
      this.send<unknown>({ method: 'GET', path: '/api/integrations', retryable: true }),
      this.send<unknown>({ method: 'GET', path: '/api/installed', retryable: true }),
    ]);
    const installed = new Map<string, Record<string, unknown>>();
    for (const row of Array.isArray(installedRows) ? installedRows : []) {
      if (row && typeof row === 'object') {
        const record = row as Record<string, unknown>;
        installed.set(String(record.integration_id), record);
      }
    }
    return (Array.isArray(catalog) ? catalog : [])
      .filter((row): row is Record<string, unknown> => !!row && typeof row === 'object')
      .map((row) => {
        const state = installed.get(String(row.id));
        const auth = Array.isArray(row.auth) ? row.auth : [];
        return {
          id: (row.id as string) ?? '',
          name: (row.name as string) ?? '',
          type: (row.type as string | undefined) ?? null,
          description: (row.description as string | undefined) ?? null,
          authMethods: auth
            .filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === 'object')
            .map((entry) => String(entry.method)),
          installed: state !== undefined,
          connected: Boolean(state?.connected),
          raw: row,
        } satisfies Integration;
      });
  }

  async installed(): Promise<Record<string, unknown>[]> {
    return (
      (await this.send<Record<string, unknown>[]>({
        method: 'GET',
        path: '/api/installed',
        retryable: true,
      })) ?? []
    );
  }

  // ── Observability (raw JSON — see the API reference for shapes) ─────────────

  /**
   * Org-wide tool-call log.
   *
   * Requires a **user** credential (`accessToken`), not an API key: these rows
   * carry every caller's tool arguments and results, so the server keeps them
   * behind a human session deliberately. With an API key this throws
   * `AuthenticationError`; use `usage()` / `usageEvents()`, which are metering
   * aggregates and are readable with a key.
   */
  async logs(query: LogsQuery = {}): Promise<Record<string, unknown>[]> {
    return (
      (await this.send<Record<string, unknown>[]>({
        method: 'GET',
        path: '/api/logs',
        query: { limit: 50, offset: 0, ...query },
        retryable: true,
      })) ?? []
    );
  }

  async usage(query: UsageQuery = {}): Promise<Record<string, unknown>> {
    return (
      (await this.send<Record<string, unknown>>({
        method: 'GET',
        path: '/api/usage/summary',
        query: { ...query },
        retryable: true,
      })) ?? {}
    );
  }

  async usageEvents(query: UsageEventsQuery = {}): Promise<Record<string, unknown>[]> {
    return (
      (await this.send<Record<string, unknown>[]>({
        method: 'GET',
        path: '/api/usage/events',
        query: { limit: 100, ...query },
        retryable: true,
      })) ?? []
    );
  }

  async deployments(): Promise<Record<string, unknown>[]> {
    return (
      (await this.send<Record<string, unknown>[]>({
        method: 'GET',
        path: '/api/deployments',
        retryable: true,
      })) ?? []
    );
  }

  // ── Transport ──────────────────────────────────────────────────────────────

  private buildUrl(prepared: Prepared): string {
    const url = new URL(`${this.baseUrl}${prepared.path}`);
    for (const [key, value] of Object.entries(prepared.query ?? {})) {
      if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
    }
    return url.toString();
  }

  private async send<T>(prepared: Prepared, context: CallContext = {}): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(
        () => controller.abort(),
        prepared.timeoutMs ?? this.timeoutMs,
      );

      let response: Response;
      try {
        const headers = { ...this.headers };
        if (prepared.body !== undefined) headers['Content-Type'] = 'application/json';
        response = await this.fetchImpl(this.buildUrl(prepared), {
          method: prepared.method,
          headers,
          body: prepared.body === undefined ? undefined : JSON.stringify(prepared.body),
          signal: controller.signal,
        });
      } catch (error) {
        throw new SutrConnectionError(
          `Could not reach ${this.baseUrl}: ${(error as Error).message}`,
        );
      } finally {
        clearTimeout(timer);
      }

      if (
        prepared.retryable &&
        RETRYABLE.has(response.status) &&
        attempt < this.maxRetries
      ) {
        const retryAfter = Number(response.headers.get('retry-after'));
        const delay = Number.isFinite(retryAfter) && retryAfter > 0
          ? Math.min(retryAfter * 1000, 30_000)
          : Math.min(500 * 2 ** attempt, 8_000);
        await sleep(delay);
        continue;
      }

      return this.interpret<T>(response, context);
    }
  }

  private async interpret<T>(response: Response, context: CallContext): Promise<T> {
    const requestId = response.headers.get('x-request-id');
    let body: unknown = null;
    if (response.status !== 204) {
      const text = await response.text();
      if (text) {
        try {
          body = JSON.parse(text);
        } catch {
          body = { detail: text.slice(0, 2000) };
        }
      }
    }

    if (response.ok) return body as T;

    // Governance responses are 403s with a machine-readable `error` code.
    if (response.status === 403 && body && typeof body === 'object') {
      const record = body as Record<string, unknown>;
      if (record.error === 'approval_required') {
        throw new ApprovalRequired(
          (record.message as string) ?? 'Tool call requires human approval.',
          (record.approval_url as string) ?? '',
          String(record.approval_request_id ?? ''),
          (record.integration_id as string) ?? context.integrationId ?? null,
          (record.tool_name as string) ?? context.toolName ?? null,
          requestId,
        );
      }
      if (record.error === 'denied') {
        throw new ToolDenied(
          (record.message as string) ?? 'This tool is blocked by policy.',
          (record.integration_id as string) ?? context.integrationId ?? null,
          (record.tool_name as string) ?? context.toolName ?? null,
          requestId,
        );
      }
    }

    const message = detailMessage(body, response.status);
    switch (true) {
      case response.status === 401:
        throw new AuthenticationError(message, response.status, body, requestId);
      case response.status === 403:
        throw new PermissionDenied(message, response.status, body, requestId);
      case response.status === 404:
        throw new NotFoundError(message, response.status, body, requestId);
      case response.status === 429: {
        const retryAfter = Number(response.headers.get('retry-after'));
        throw new RateLimited(
          message,
          response.status,
          body,
          requestId,
          Number.isFinite(retryAfter) ? retryAfter : null,
        );
      }
      case response.status >= 500:
        throw new ServerError(message, response.status, body, requestId);
      default:
        throw new APIError(message, response.status, body, requestId);
    }
  }
}
