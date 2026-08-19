/** Public types. Each response type keeps the parsed payload in `raw` so a new
 *  server field is never unreachable through the SDK. */

export interface SutrOptions {
  /** API key (create one under Settings → API Keys). Falls back to SUTR_API_KEY. */
  apiKey?: string;
  /** Session JWT, as an alternative to an API key. */
  accessToken?: string;
  /** Defaults to SUTR_BASE_URL, then https://app.sutr.sh */
  baseUrl?: string;
  /** Per-request timeout in ms (default 30_000). */
  timeoutMs?: number;
  /** Automatic retries for idempotent reads only (default 2). */
  maxRetries?: number;
  /** Injected in tests; defaults to global fetch. */
  fetch?: typeof globalThis.fetch;
}

export interface JsonSchema {
  type?: string;
  properties?: Record<string, Record<string, unknown>>;
  required?: string[];
  [key: string]: unknown;
}

export interface Tool {
  name: string;
  description: string | null;
  inputSchema: JsonSchema;
  integrationId: string | null;
  /** "allow" | "require_approval" | "deny" — the policy at listing time. */
  executionMode: string | null;
  category: string | null;
  raw: Record<string, unknown>;
}

export interface ContentBlock {
  type?: string;
  text?: string;
  [key: string]: unknown;
}

export interface ToolResult {
  content: ContentBlock[];
  isError: boolean;
  statusCode: number | null;
  durationMs: number | null;
  /** All text blocks joined — usually what you hand back to a model. */
  text: string;
  raw: Record<string, unknown>;
}

export type ApprovalStatus =
  | 'pending'
  | 'approved'
  | 'denied'
  | 'expired'
  | 'consumed'
  | 'auto_approved'
  | (string & {});

export interface ApprovalDecision {
  approvalRequestId: string;
  integrationId: string;
  toolName: string;
  status: ApprovalStatus;
  message: string;
  expiresAt: string | null;
  decisionMode: string | null;
  approved: boolean;
  pending: boolean;
  raw: Record<string, unknown>;
}

export interface Integration {
  id: string;
  name: string;
  type: string | null;
  description: string | null;
  authMethods: string[];
  installed: boolean;
  connected: boolean;
  raw: Record<string, unknown>;
}

export interface CallToolOptions {
  /** Explanation shown to the human reviewer and recorded in the audit trail. */
  additionalInfo?: string;
  /** Block until a human decides, then retry the identical call. */
  waitForApproval?: boolean;
  /** Milliseconds to keep waiting; omit to wait indefinitely. */
  approvalTimeoutMs?: number;
}

export interface LogsQuery {
  integration?: string;
  tool?: string;
  outcome?: string;
  limit?: number;
  offset?: number;
}

export interface UsageQuery {
  start?: string;
  end?: string;
}

export interface UsageEventsQuery extends UsageQuery {
  kind?: string;
  limit?: number;
}
