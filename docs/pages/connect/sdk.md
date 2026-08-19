# SDKs

Official SDKs for calling governed tools from your own code. Both wrap the same
REST API, expose the same surface, and use the same error names, so switching
languages does not mean relearning the model.

| Language | Package | Source |
|---|---|---|
| Python | `sutr-sdk` (`pip install sutr-sdk`) | [`sdk/python`](https://github.com/sutr-dev/sutr/tree/main/sdk/python) |
| TypeScript / JavaScript | `@sutr/sdk` (`npm install @sutr/sdk`) | [`sdk/typescript`](https://github.com/sutr-dev/sutr/tree/main/sdk/typescript) |

The point of using an SDK rather than raw HTTP is the approval flow: a gated
tool is a *typed outcome*, not an opaque 403, and waiting for a human decision
is one argument rather than a polling loop you write yourself.

## Credentials

Create an API key in the UI under **Settings → API Keys**, then either pass it
explicitly or set `SUTR_API_KEY` (and `SUTR_BASE_URL` for a self-hosted
instance). The upstream service credentials stay on the Sutr server — your
agent code never holds them.

```python
from sutr_sdk import Sutr

sutr = Sutr(api_key="ap_...", base_url="https://sutr.example.com")
```

```ts
import { Sutr } from '@sutr/sdk';

const sutr = new Sutr({ apiKey: 'ap_...', baseUrl: 'https://sutr.example.com' });
```

## Calling a tool

```python
tools = sutr.list_tools()                 # every installed integration
result = sutr.call_tool("posthog", "create_annotation", {"content": "shipped"})
print(result.text)                        # text blocks joined
```

```ts
const tools = await sutr.listTools();
const result = await sutr.callTool('posthog', 'create_annotation', { content: 'shipped' });
console.log(result.text);
```

## Approvals

A tool in *require approval* mode raises `ApprovalRequired`, carrying the URL to
hand a human. A tool in *deny* mode raises `ToolDenied` — waiting will not help.

```python
result = sutr.call_tool(
    "stripe", "create_refund", {"charge": "ch_123"},
    additional_info="Customer reported a duplicate charge (ticket 4821)",
    wait_for_approval=True,
    approval_timeout=600,
)
```

```ts
const result = await sutr.callTool('stripe', 'create_refund', { charge: 'ch_123' }, {
  additionalInfo: 'Customer reported a duplicate charge (ticket 4821)',
  waitForApproval: true,
  approvalTimeoutMs: 600_000,
});
```

`additional_info` / `additionalInfo` is shown on the approval screen and stored
in the audit trail — give the reviewer the reason, not just the arguments.

When the human approves, the SDK re-issues the **identical** request, because an
approval is bound to the exact arguments that were shown. See
[Tool approvals](../tool-approvals) for the policy model.

## Reads

`integrations()`, `installed()`, `usage()`, `usage_events()`, and
`deployments()` work with an API key. `logs()` needs a **user** credential
(`access_token` / `accessToken`) because log rows contain every caller's tool
arguments and results:

```python
with Sutr(access_token=session_jwt, base_url=...) as human:
    human.logs(outcome="error", limit=20)
```

## Reliability

Idempotent reads retry automatically on 429/502/503/504, honouring
`Retry-After`. **Tool calls never retry** — they have side effects, and a
duplicate refund is worse than a visible error. Every exception inherits from
`SutrError` and carries the server's `request_id` when present.

Full method-by-method documentation lives in each package's README.
