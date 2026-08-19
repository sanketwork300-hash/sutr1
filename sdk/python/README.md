# Sutr Python SDK

Call **governed** tools from Python. Every call goes through your Sutr
instance, which applies the per-tool approval policy, records the call in the
audit log and usage ledger, and injects the upstream credential server-side —
so your agent code never holds an API key for the services it uses.

```sh
pip install sutr-sdk
```

> The import package is `sutr_sdk`; the distribution is `sutr-sdk`. Both differ
> from the Sutr *server* package (`sutr`) on purpose, so the two can be
> installed side by side.

## Quick start

```python
from sutr_sdk import Sutr

with Sutr(api_key="ap_...", base_url="https://sutr.example.com") as sutr:
    for tool in sutr.list_tools():
        print(tool.integration_id, tool.name, tool.execution_mode)

    result = sutr.call_tool("posthog", "create_annotation", {"content": "shipped"})
    print(result.text)
```

`api_key` and `base_url` fall back to `SUTR_API_KEY` and `SUTR_BASE_URL`.
Create a key in the Sutr UI under **Settings → API Keys**.

## The approval flow

A tool set to *require approval* does not execute until a human says so. That
is a normal outcome, not a failure, so the SDK models it as a typed exception:

```python
from sutr_sdk import ApprovalRequired, ToolDenied

try:
    result = sutr.call_tool("stripe", "create_refund", {"charge": "ch_123"})
except ApprovalRequired as gate:
    print(f"A human needs to approve this: {gate.approval_url}")
    decision = sutr.await_approval(gate.approval_request_id, timeout=600)
    if decision.approved:
        result = sutr.call_tool("stripe", "create_refund", {"charge": "ch_123"})
except ToolDenied:
    print("Policy blocks this tool outright — waiting will not help.")
```

Or let the SDK do it in one call:

```python
result = sutr.call_tool(
    "stripe",
    "create_refund",
    {"charge": "ch_123"},
    additional_info="Customer reported a duplicate charge (ticket 4821)",
    wait_for_approval=True,  # blocks until a human decides
    approval_timeout=600,  # raises ApprovalPending if nobody decides
)
```

Pass `additional_info` whenever the agent has a reason worth showing the
reviewer — it appears on the approval screen and in the audit trail.

The retried call is byte-identical to the original, because an approval is
bound to the exact arguments a human saw.

## Async

Same surface, awaited:

```python
from sutr_sdk import AsyncSutr

async with AsyncSutr() as sutr:
    tools = await sutr.list_tools("posthog")
    result = await sutr.call_tool("posthog", "list_projects", {})
```

## Other reads

```python
sutr.integrations()  # catalog, annotated with what's installed
sutr.installed()
sutr.usage(start="2026-08-01T00:00:00Z")
sutr.usage_events(kind="tool_call")
sutr.deployments()
```

`logs()` is the exception: it needs a **user** credential, because the log rows
carry every caller's tool arguments and results.

```python
with Sutr(access_token=session_jwt, base_url=...) as human:
    human.logs(outcome="error", limit=20)
```

With an API key it raises `AuthenticationError` by design — reach for
`usage()`/`usage_events()`, which are metering aggregates and key-readable.

These return parsed JSON as documented in the
[API reference](https://docs.sutr.sh/api), so a new server field is never
unreachable through the SDK. The typed models (`Tool`, `ToolResult`,
`ApprovalDecision`, `Integration`) each keep their source dict in `.raw` for the
same reason.

## Errors

| Exception | Meaning |
|---|---|
| `ApprovalRequired` | A human must decide first. Carries `approval_url`, `approval_request_id`. |
| `ApprovalPending` | You waited, nobody decided yet. Try again later. |
| `ToolDenied` | Policy is `deny`. Not retryable. |
| `AuthenticationError` | Missing/invalid/revoked credential (401). |
| `PermissionDenied` | Your role forbids the action (403). |
| `NotFoundError` | No such integration, tool, or resource (404). |
| `RateLimited` | Too many requests (429); `retry_after` when the server said so. |
| `ServerError` | 5xx. |
| `SutrConnectionError` | The server was unreachable. |

All inherit from `SutrError` and carry `request_id` when the server sent one —
quote it when reporting a problem.

Idempotent reads are retried automatically on 429/502/503/504 (honouring
`Retry-After`). **Tool calls are never retried** — they have side effects.

## Development

```sh
cd sdk/python
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```
