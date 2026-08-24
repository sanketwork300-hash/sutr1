# Sutr TypeScript SDK

Call **governed** tools from TypeScript or JavaScript. Every call goes through
your Sutr instance, which applies the per-tool approval policy, records the call
in the audit log and usage ledger, and injects the upstream credential
server-side — so your agent code never holds an API key for the services it uses.

```sh
npm install @sutr/sdk
```

Zero runtime dependencies; uses the platform `fetch` (Node 18+, Deno, Bun, or a
browser — though a browser should never hold a Sutr API key).

## Quick start

```ts
import { Sutr } from '@sutr/sdk';

const sutr = new Sutr({ apiKey: 'ap_...', baseUrl: 'https://sutr.example.com' });

for (const tool of await sutr.listTools()) {
  console.log(tool.integrationId, tool.name, tool.executionMode);
}

const result = await sutr.callTool('posthog', 'create_annotation', { content: 'shipped' });
console.log(result.text);
```

`apiKey` and `baseUrl` fall back to `SUTR_API_KEY` and `SUTR_BASE_URL`. Create a
key in the Sutr UI under **Develop → API Keys**.

## The approval flow

A tool set to *require approval* does not execute until a human says so. That is
a normal outcome, not a failure, so it arrives as a typed error:

```ts
import { ApprovalRequired, ToolDenied } from '@sutr/sdk';

try {
  const result = await sutr.callTool('stripe', 'create_refund', { charge: 'ch_123' });
} catch (error) {
  if (error instanceof ApprovalRequired) {
    console.log(`A human needs to approve this: ${error.approvalUrl}`);
    const decision = await sutr.awaitApproval(error.approvalRequestId, { timeoutMs: 600_000 });
    if (decision.approved) {
      /* re-issue the identical call */
    }
  } else if (error instanceof ToolDenied) {
    console.log('Policy blocks this tool outright — waiting will not help.');
  } else {
    throw error;
  }
}
```

Or let the SDK handle it:

```ts
const result = await sutr.callTool(
  'stripe',
  'create_refund',
  { charge: 'ch_123' },
  {
    additionalInfo: 'Customer reported a duplicate charge (ticket 4821)',
    waitForApproval: true,     // blocks until a human decides
    approvalTimeoutMs: 600_000 // throws ApprovalPending if nobody decides
  },
);
```

Pass `additionalInfo` whenever the agent has a reason worth showing the reviewer —
it appears on the approval screen and in the audit trail.

The retried call is byte-identical to the original, because an approval is bound
to the exact arguments a human saw.

## Other reads

```ts
await sutr.integrations();               // catalog, annotated with what's installed
await sutr.installed();
await sutr.usage({ start: '2026-08-01T00:00:00Z' });
await sutr.usageEvents({ kind: 'tool_call' });
await sutr.deployments();
```

`logs()` is the exception: it needs a **user** credential, because the log rows
carry every caller's tool arguments and results.

```ts
const human = new Sutr({ accessToken: sessionJwt, baseUrl });
await human.logs({ outcome: 'error', limit: 20 });
```

With an API key it throws `AuthenticationError` by design — reach for
`usage()` / `usageEvents()`, which are metering aggregates and key-readable.

These return parsed JSON as documented in the
[API reference](https://docs.sutr.sh/api), so a new server field is never
unreachable through the SDK. Typed results also keep the payload in `.raw`.

## Errors

| Class | Meaning |
|---|---|
| `ApprovalRequired` | A human must decide first. Carries `approvalUrl`, `approvalRequestId`. |
| `ApprovalPending` | You waited, nobody decided yet. Try again later. |
| `ToolDenied` | Policy is `deny`. Not retryable. |
| `AuthenticationError` | Missing/invalid/revoked credential (401). |
| `PermissionDenied` | Your role forbids the action (403). |
| `NotFoundError` | No such integration, tool, or resource (404). |
| `RateLimited` | Too many requests (429); `retryAfter` when the server said so. |
| `ServerError` | 5xx. |
| `SutrConnectionError` | The server was unreachable. |

All extend `SutrError` and carry `requestId` when the server sent one — quote it
when reporting a problem.

Idempotent reads are retried automatically on 429/502/503/504 (honouring
`Retry-After`). **Tool calls are never retried** — they have side effects.

## Development

```sh
cd sdk/typescript
pnpm install
pnpm test        # vitest
pnpm typecheck
pnpm build
```
