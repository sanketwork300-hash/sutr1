/**
 * Error hierarchy, mirroring the Python SDK's names so the two read alike.
 *
 * `ApprovalRequired`, `ApprovalPending`, and `ToolDenied` deliberately lack an
 * "Error" suffix: they are expected outcomes of Sutr's governance model, not
 * faults. Every error carries the server's `X-Request-ID` when present, so a
 * user can quote it and an operator can find the matching log line.
 */

export class SutrError extends Error {
  readonly requestId: string | null;

  constructor(message: string, requestId: string | null = null) {
    super(requestId ? `${message} (requestId=${requestId})` : message);
    this.name = new.target.name;
    this.requestId = requestId;
  }
}

export class SutrConnectionError extends SutrError {}

export class APIError extends SutrError {
  readonly status: number;
  readonly body: unknown;

  constructor(message: string, status: number, body: unknown, requestId: string | null = null) {
    super(message, requestId);
    this.status = status;
    this.body = body;
  }
}

/** 401 — missing, wrong, or revoked credential. */
export class AuthenticationError extends APIError {}

/** 403 — authenticated, but the caller's role forbids the action. */
export class PermissionDenied extends APIError {}

/** 404 — no such integration, tool, or resource. */
export class NotFoundError extends APIError {}

/** 429 — too many requests. */
export class RateLimited extends APIError {
  readonly retryAfter: number | null;

  constructor(
    message: string,
    status: number,
    body: unknown,
    requestId: string | null = null,
    retryAfter: number | null = null,
  ) {
    super(message, status, body, requestId);
    this.retryAfter = retryAfter;
  }
}

/** 5xx. */
export class ServerError extends APIError {}

/** The tool's policy is `deny`. Not retryable — waiting will not help. */
export class ToolDenied extends SutrError {
  readonly integrationId: string | null;
  readonly toolName: string | null;

  constructor(
    message: string,
    integrationId: string | null = null,
    toolName: string | null = null,
    requestId: string | null = null,
  ) {
    super(message, requestId);
    this.integrationId = integrationId;
    this.toolName = toolName;
  }
}

/**
 * A human must approve the call first. Share `approvalUrl`, then either
 * `awaitApproval(approvalRequestId)` or re-issue with `waitForApproval: true`.
 */
export class ApprovalRequired extends SutrError {
  readonly approvalUrl: string;
  readonly approvalRequestId: string;
  readonly integrationId: string | null;
  readonly toolName: string | null;

  constructor(
    message: string,
    approvalUrl: string,
    approvalRequestId: string,
    integrationId: string | null = null,
    toolName: string | null = null,
    requestId: string | null = null,
  ) {
    super(message, requestId);
    this.approvalUrl = approvalUrl;
    this.approvalRequestId = approvalRequestId;
    this.integrationId = integrationId;
    this.toolName = toolName;
  }
}

/** Waiting timed out client-side without a decision. Try again later. */
export class ApprovalPending extends SutrError {
  readonly approvalUrl: string | null;
  readonly approvalRequestId: string | null;

  constructor(
    message: string,
    approvalUrl: string | null = null,
    approvalRequestId: string | null = null,
  ) {
    super(message);
    this.approvalUrl = approvalUrl;
    this.approvalRequestId = approvalRequestId;
  }
}
