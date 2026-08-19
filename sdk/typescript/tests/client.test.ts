import { describe, expect, it, vi } from 'vitest';
import {
  ApprovalPending,
  ApprovalRequired,
  AuthenticationError,
  NotFoundError,
  PermissionDenied,
  RateLimited,
  ServerError,
  Sutr,
  SutrConnectionError,
  SutrError,
  ToolDenied,
} from '../src/index.js';

const BASE = 'https://sutr.test';
const KEY = 'ap_testkey';

const TOOL_RESULT = { content: [{ type: 'text', text: 'done' }], isError: false, duration_ms: 12 };
const APPROVAL_BODY = {
  error: 'approval_required',
  approval_request_id: '11111111-1111-1111-1111-111111111111',
  approval_url: `${BASE}/approve/11111111-1111-1111-1111-111111111111`,
  message: 'Tool call requires approval before execution.',
  integration_id: 'posthog',
  tool_name: 'create_annotation',
};

interface Recorded {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

type Scripted = { status?: number; json?: unknown; text?: string; headers?: Record<string, string> };

/** Scripted fetch: records calls, replays queued responses. */
function scriptedFetch(...responses: (Scripted | Error)[]) {
  const calls: Recorded[] = [];
  const queue = [...responses];

  const fetchImpl = (async (input: string | URL, init?: RequestInit) => {
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries((init?.headers ?? {}) as Record<string, string>)) {
      headers[key.toLowerCase()] = value;
    }
    calls.push({
      url: String(input),
      method: init?.method ?? 'GET',
      headers,
      body: typeof init?.body === 'string' ? init.body : null,
    });

    const next = queue.shift() ?? { status: 200, json: {} };
    if (next instanceof Error) throw next;
    const payload = next.text ?? (next.json === undefined ? '' : JSON.stringify(next.json));
    return new Response(payload || null, {
      status: next.status ?? 200,
      headers: { 'content-type': 'application/json', ...(next.headers ?? {}) },
    });
  }) as unknown as typeof globalThis.fetch;

  return { fetchImpl, calls };
}

function makeClient(...responses: (Scripted | Error)[]) {
  const { fetchImpl, calls } = scriptedFetch(...responses);
  const sutr = new Sutr({ apiKey: KEY, baseUrl: BASE, fetch: fetchImpl, maxRetries: 2 });
  return { sutr, calls };
}

function paths(calls: Recorded[]): string[] {
  return calls.map((call) => new URL(call.url).pathname);
}

/** Await a call that must reject, returning the typed error. */
async function captureError<T>(promise: Promise<unknown>): Promise<T> {
  try {
    await promise;
  } catch (error) {
    return error as T;
  }
  throw new Error('expected the call to reject, but it resolved');
}

describe('construction', () => {
  it('requires credentials', () => {
    const previous = process.env.SUTR_API_KEY;
    delete process.env.SUTR_API_KEY;
    try {
      expect(() => new Sutr()).toThrow(/No credentials/);
    } finally {
      if (previous !== undefined) process.env.SUTR_API_KEY = previous;
    }
  });

  it('reads credentials and base URL from the environment', () => {
    process.env.SUTR_API_KEY = 'ap_env';
    process.env.SUTR_BASE_URL = 'https://env.example.com/';
    try {
      expect(new Sutr().baseUrl).toBe('https://env.example.com'); // trailing slash trimmed
    } finally {
      delete process.env.SUTR_API_KEY;
      delete process.env.SUTR_BASE_URL;
    }
  });

  it('sends the API key and a versioned user agent', async () => {
    const { sutr, calls } = makeClient({ json: [] });
    await sutr.listTools();
    expect(calls[0]!.headers['x-api-key']).toBe(KEY);
    expect(calls[0]!.headers['user-agent']).toContain('sutr-sdk-js');
  });

  it('supports a bearer token instead of an API key', async () => {
    const { fetchImpl, calls } = scriptedFetch({ json: [] });
    const sutr = new Sutr({ accessToken: 'jwt', baseUrl: BASE, fetch: fetchImpl });
    await sutr.listTools();
    expect(calls[0]!.headers.authorization).toBe('Bearer jwt');
    expect(calls[0]!.headers['x-api-key']).toBeUndefined();
  });

  it('lets an explicit access token beat an ambient SUTR_API_KEY', async () => {
    // A key cannot read user-scoped endpoints, so silently swapping the
    // credential would break logs() in a way that is hard to diagnose.
    process.env.SUTR_API_KEY = 'ap_ambient';
    try {
      const { fetchImpl, calls } = scriptedFetch({ json: [] });
      const sutr = new Sutr({ accessToken: 'jwt', baseUrl: BASE, fetch: fetchImpl });
      await sutr.listTools();
      expect(calls[0]!.headers.authorization).toBe('Bearer jwt');
      expect(calls[0]!.headers['x-api-key']).toBeUndefined();
    } finally {
      delete process.env.SUTR_API_KEY;
    }
  });

  it('still falls back to the env key when nothing is passed', async () => {
    process.env.SUTR_API_KEY = 'ap_ambient';
    try {
      const { fetchImpl, calls } = scriptedFetch({ json: [] });
      const sutr = new Sutr({ baseUrl: BASE, fetch: fetchImpl });
      await sutr.listTools();
      expect(calls[0]!.headers['x-api-key']).toBe('ap_ambient');
    } finally {
      delete process.env.SUTR_API_KEY;
    }
  });
});

describe('tools', () => {
  it('lists tools and stamps the integration id', async () => {
    const { sutr, calls } = makeClient({
      json: [
        {
          name: 'create_annotation',
          description: 'Create one',
          inputSchema: { type: 'object', required: ['content'] },
          execution_mode: 'require_approval',
        },
      ],
    });
    const tools = await sutr.listTools('posthog');

    expect(paths(calls)).toEqual(['/api/tools/posthog']);
    expect(tools[0]!.name).toBe('create_annotation');
    expect(tools[0]!.integrationId).toBe('posthog');
    expect(tools[0]!.executionMode).toBe('require_approval');
    expect(tools[0]!.raw.description).toBe('Create one');
  });

  it('lists all tools from the collection endpoint', async () => {
    const { sutr, calls } = makeClient({ json: [] });
    await sutr.listTools();
    expect(paths(calls)).toEqual(['/api/tools']);
  });

  it('url-encodes integration ids', async () => {
    const { sutr, calls } = makeClient({ json: TOOL_RESULT });
    await sutr.callTool('weird/id space', 't');
    expect(calls[0]!.url).toContain('/api/tools/weird%2Fid%20space/call');
  });

  it('returns a result with joined text', async () => {
    const { sutr, calls } = makeClient({ json: TOOL_RESULT });
    const result = await sutr.callTool('posthog', 'create_annotation', { content: 'hi' });

    expect(result.text).toBe('done');
    expect(result.isError).toBe(false);
    expect(result.durationMs).toBe(12);
    expect(JSON.parse(calls[0]!.body!)).toEqual({
      tool_name: 'create_annotation',
      args: { content: 'hi' },
    });
  });

  it('joins multiple text blocks and skips non-text', async () => {
    const { sutr } = makeClient({ json: { content: [{ text: 'a' }, { type: 'image' }, { text: 'b' }] } });
    expect((await sutr.callTool('i', 't')).text).toBe('a\nb');
  });

  it('forwards additionalInfo only when provided', async () => {
    const { sutr, calls } = makeClient({ json: TOOL_RESULT }, { json: TOOL_RESULT });
    await sutr.callTool('posthog', 't', {}, { additionalInfo: 'because reasons' });
    await sutr.callTool('posthog', 't', {});
    expect(calls[0]!.body).toContain('because reasons');
    expect(calls[1]!.body).not.toContain('additional_info');
  });
});

describe('governance responses', () => {
  it('throws ApprovalRequired with the url and request id', async () => {
    const { sutr } = makeClient({ status: 403, json: APPROVAL_BODY });
    await expect(sutr.callTool('posthog', 'create_annotation', {})).rejects.toThrow(
      ApprovalRequired,
    );

    const { sutr: again } = makeClient({ status: 403, json: APPROVAL_BODY });
    const error = await captureError<ApprovalRequired>(
      again.callTool('posthog', 'create_annotation', {}),
    );
    expect(error.approvalRequestId).toBe(APPROVAL_BODY.approval_request_id);
    expect(error.approvalUrl).toBe(APPROVAL_BODY.approval_url);
    expect(error.toolName).toBe('create_annotation');
  });

  it('throws ToolDenied for a policy denial', async () => {
    const { sutr } = makeClient({
      status: 403,
      json: { error: 'denied', message: 'Blocked.', tool_name: 'nuke' },
    });
    const error = await captureError<ToolDenied>(sutr.callTool('posthog', 'nuke'));
    expect(error).toBeInstanceOf(ToolDenied);
    expect(error.toolName).toBe('nuke');
  });

  it('distinguishes a role-based 403 from a policy denial', async () => {
    const { sutr } = makeClient({
      status: 403,
      json: { detail: { error: 'permission_denied', message: 'Your role does not allow this.' } },
    });
    await expect(sutr.callTool('posthog', 't')).rejects.toThrow(PermissionDenied);
  });

  it('waits for approval then retries the identical call', async () => {
    const { sutr, calls } = makeClient(
      { status: 403, json: APPROVAL_BODY },
      { json: { ...APPROVAL_BODY, status: 'approved', message: 'ok' } },
      { json: TOOL_RESULT },
    );
    const result = await sutr.callTool(
      'posthog',
      'create_annotation',
      { content: 'hi' },
      { waitForApproval: true },
    );

    expect(result.text).toBe('done');
    expect(paths(calls)).toEqual([
      '/api/tools/posthog/call',
      `/api/tool-approvals/requests/${APPROVAL_BODY.approval_request_id}/await`,
      '/api/tools/posthog/call',
    ]);
    // Byte-identical retry, or the approval's argument hash won't match.
    expect(calls[2]!.body).toBe(calls[0]!.body);
  });

  it('throws on denial while waiting', async () => {
    const { sutr } = makeClient(
      { status: 403, json: APPROVAL_BODY },
      { json: { ...APPROVAL_BODY, status: 'denied', message: 'Denied by human' } },
    );
    await expect(
      sutr.callTool('posthog', 'create_annotation', {}, { waitForApproval: true }),
    ).rejects.toThrow(/Denied by human/);
  });

  it('throws ApprovalPending when the wait times out', async () => {
    const pending = { json: { ...APPROVAL_BODY, status: 'pending', message: 'waiting' } };
    const { sutr } = makeClient({ status: 403, json: APPROVAL_BODY }, pending, pending, pending);
    await expect(
      sutr.callTool('posthog', 'create_annotation', {}, {
        waitForApproval: true,
        approvalTimeoutMs: 10,
      }),
    ).rejects.toThrow(ApprovalPending);
  });

  it('sends the remaining window when a timeout is set, and none otherwise', async () => {
    const approved = { json: { ...APPROVAL_BODY, status: 'approved', message: 'ok' } };
    const { sutr, calls } = makeClient(approved, approved);
    await sutr.awaitApproval(APPROVAL_BODY.approval_request_id, { timeoutMs: 30_000 });
    await sutr.awaitApproval(APPROVAL_BODY.approval_request_id);
    expect(JSON.parse(calls[0]!.body!)).toEqual({ timeout_seconds: 30 });
    expect(calls[1]!.body).toBeNull();
  });

  it('spaces out instant pending replies instead of hot-looping', async () => {
    const pending = { json: { ...APPROVAL_BODY, status: 'pending', message: 'waiting' } };
    const { sutr, calls } = makeClient(pending, {
      json: { ...APPROVAL_BODY, status: 'approved', message: 'ok' },
    });
    const started = Date.now();
    const decision = await sutr.awaitApproval(APPROVAL_BODY.approval_request_id);
    expect(decision.approved).toBe(true);
    expect(calls).toHaveLength(2);
    expect(Date.now() - started).toBeGreaterThanOrEqual(400); // poll floor honoured
  });
});

describe('error mapping', () => {
  const cases: [number, unknown][] = [
    [401, AuthenticationError],
    [404, NotFoundError],
    [429, RateLimited],
    [500, ServerError],
    [503, ServerError],
  ];

  for (const [status, expected] of cases) {
    it(`maps ${status}`, async () => {
      const { fetchImpl } = scriptedFetch({ status, json: { detail: 'boom' } });
      // maxRetries: 0 so transient codes surface instead of being retried away.
      const sutr = new Sutr({ apiKey: KEY, baseUrl: BASE, fetch: fetchImpl, maxRetries: 0 });
      const error = await captureError<ServerError>(sutr.listTools());
      expect(error).toBeInstanceOf(expected as never);
      expect(error.status).toBe(status);
      expect(error.message).toContain('boom');
    });
  }

  it('attaches the request id for support', async () => {
    const { fetchImpl } = scriptedFetch({
      status: 500,
      json: { detail: 'Internal Server Error' },
      headers: { 'x-request-id': 'abc123' },
    });
    const sutr = new Sutr({ apiKey: KEY, baseUrl: BASE, fetch: fetchImpl, maxRetries: 0 });
    const error = await captureError<ServerError>(sutr.listTools());
    expect(error.requestId).toBe('abc123');
    expect(error.message).toContain('abc123');
  });

  it('summarises validation errors', async () => {
    const { sutr } = makeClient({
      status: 422,
      json: { detail: [{ loc: ['body', 'tool_name'], msg: 'field required' }] },
    });
    await expect(sutr.callTool('i', 't')).rejects.toThrow(/tool_name: field required/);
  });

  it('wraps connection failures', async () => {
    const { sutr } = makeClient(new TypeError('fetch failed'));
    await expect(sutr.listTools()).rejects.toThrow(SutrConnectionError);
  });

  it('tolerates a non-JSON error body', async () => {
    const { fetchImpl } = scriptedFetch({ status: 502, text: '<html>bad gateway</html>' });
    const sutr = new Sutr({ apiKey: KEY, baseUrl: BASE, fetch: fetchImpl, maxRetries: 0 });
    await expect(sutr.listTools()).rejects.toThrow(ServerError);
  });

  it('exposes every error under SutrError', async () => {
    const { sutr } = makeClient({ status: 401, json: { detail: 'nope' } });
    await expect(sutr.listTools()).rejects.toThrow(SutrError);
  });
});

describe('retries', () => {
  it('retries idempotent reads', async () => {
    vi.useFakeTimers();
    try {
      const { sutr, calls } = makeClient({ status: 503, json: { detail: 'down' } }, { json: [] });
      const promise = sutr.listTools();
      await vi.runAllTimersAsync();
      expect(await promise).toEqual([]);
      expect(calls).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('never retries tool calls', async () => {
    // A retried call could execute the side effect twice.
    const { sutr, calls } = makeClient(
      { status: 503, json: { detail: 'down' } },
      { json: TOOL_RESULT },
    );
    await expect(sutr.callTool('posthog', 'create_annotation', {})).rejects.toThrow(ServerError);
    expect(calls).toHaveLength(1);
  });

  it('gives up after maxRetries', async () => {
    vi.useFakeTimers();
    try {
      const { fetchImpl, calls } = scriptedFetch(
        ...Array.from({ length: 5 }, () => ({ status: 503, json: { detail: 'down' } })),
      );
      const sutr = new Sutr({ apiKey: KEY, baseUrl: BASE, fetch: fetchImpl, maxRetries: 1 });
      const promise = sutr.listTools();
      const assertion = expect(promise).rejects.toThrow(ServerError);
      await vi.runAllTimersAsync();
      await assertion;
      expect(calls).toHaveLength(2); // original + one retry
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('other endpoints', () => {
  it('merges installed state into the catalog', async () => {
    const { sutr, calls } = makeClient(
      {
        json: [
          { id: 'posthog', name: 'PostHog', type: 'remote_mcp', auth: [{ method: 'token' }] },
          { id: 'github', name: 'GitHub', type: 'remote_mcp', auth: [{ method: 'oauth' }] },
        ],
      },
      { json: [{ integration_id: 'posthog', connected: true }] },
    );
    const result = await sutr.integrations();

    expect(paths(calls).sort()).toEqual(['/api/installed', '/api/integrations']);
    const byId = Object.fromEntries(result.map((i) => [i.id, i]));
    expect(byId.posthog!.installed).toBe(true);
    expect(byId.posthog!.connected).toBe(true);
    expect(byId.github!.installed).toBe(false);
    expect(byId.github!.authMethods).toEqual(['oauth']);
  });

  it('passes query filters and drops undefined ones', async () => {
    const { sutr, calls } = makeClient({ json: { tool_calls: 7 } }, { json: [] });
    expect((await sutr.usage({ start: '2026-01-01T00:00:00Z' })).tool_calls).toBe(7);
    await sutr.logs({ integration: 'posthog', outcome: 'executed', limit: 5 });

    expect(new URL(calls[0]!.url).searchParams.get('start')).toBe('2026-01-01T00:00:00Z');
    const params = new URL(calls[1]!.url).searchParams;
    expect(params.get('integration')).toBe('posthog');
    expect(params.get('limit')).toBe('5');
    expect(params.has('tool')).toBe(false);
  });

  it('reads usage events and deployments', async () => {
    const { sutr, calls } = makeClient({ json: [{ kind: 'tool_call' }] }, { json: [] });
    expect((await sutr.usageEvents({ kind: 'tool_call' }))[0]!.kind).toBe('tool_call');
    await sutr.deployments();
    expect(paths(calls)).toEqual(['/api/usage/events', '/api/deployments']);
  });
});
