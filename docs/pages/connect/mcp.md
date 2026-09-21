---
title: Connecting via MCP
nav_title: MCP
---

# Connecting via MCP

Sutr exposes an MCP gateway to every integration you have installed. Connecting your agent to it allows the agent to install integrations and call tools on any integration you have installed, always according to your defined approval policies.

## Endpoint

Three transports, all serving the **same** gateway — the same tools, the same approval policies, the
same logging and metering. Which one you use is a matter of what your client speaks.

| Transport | Endpoint | Use it when |
|-----------|----------|-------------|
| Streamable HTTP (**default**) | `POST /mcp` | Your client supports it. This is the current MCP transport and the only one with server-pushed `tools/list_changed` notifications. |
| SSE | `GET /sse`, then `POST /messages?session_id=…` | Your client only speaks the older SSE transport. |
| stdio | `sutr-mcp-stdio` (a subprocess) | Your client can only launch a subprocess and cannot open an HTTP session. |

| Deployment | Base |
|------------|-----|
| Cloud | `https://app.sutr.sh` |
| Self-hosted | `https://<your_domain>` |

### SSE

`GET /sse` opens the event stream and announces the endpoint to post to; that endpoint carries a
session id. Both requests are authenticated the same way as `/mcp`, and a `POST /messages` is
additionally checked against the organization that opened the session — knowing a session id is not
enough to inject into someone else's stream.

Set `MCP_SSE_ENABLED=false` to turn the SSE transport off.

### stdio

```sh
SUTR_API_KEY=ap_... sutr-mcp-stdio
```

Identity comes from `SUTR_API_KEY` in the environment rather than an argument, because an argument
ends up in the process table where anything on the machine can read it. This is a **single-tenant**
transport by construction: one process, one key, one organization. It is not a way to serve several
tenants.


## Authentication

The MCP can authenticate via OAuth or an API key. We recommend authenticating using OAuth and most clients will automatically walk you through this. 


### Getting an API key

You can get an API key from the "Connect" page in the Sutr UI. These keys allow installing integrations and calling tools but not updating policies, seeing logs, etc.

## Tools


| Tool | Purpose |
|------|---------|
| `sutr__list_installed_integrations` | List integrations installed for your user |
| `sutr__list_available_integrations` | Show all integrations that are available to be installed |
| `sutr__install_integration` | Start the auth flow to authenticate to the integration and install it |
| `sutr__get_auth_status` | Get auth status for a given integration |
| `sutr__list_integration_tools` | List or search tools across installed integrations |
| `sutr__describe_tool` | Returns input schema and current approval policy for a given tool |
| `sutr__call_tool` | Invoke an upstream tool (Stripe, GitHub, Gmail, ...) |
| `sutr__await_approval` | Long-poll for the human's decision on an approval-gated call |


## Connect your agent

### The short way

If you have the [CLI](/connect/cli) installed and authenticated, one command verifies the
connection and writes the entry into the agent's own config file:

```sh
sutr connect claude-code     # or claude-desktop, cursor, vscode, codex
sutr connect --list          # what it can configure, and where
```

It prints the endpoint, the tool count and the policy split, backs up the file it touches, and
ends by offering the optional [Sutr Skills](/connect/skills) install. See
[Connecting an agent](/connect/cli#connecting-an-agent) for the flags, the non-interactive
behaviour and the failure modes.

### By hand

All clients use the same shape: a single `sutr` MCP server pointing at the URL above with the key in an `X-API-Key` header.

### Claude Desktop

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sutr": {
      "url": "https://app.sutr.sh/mcp",
      "headers": {
        "X-API-Key": "ap_your_key_here"
      }
    }
  }
}
```

Restart Claude Desktop. The `sutr__*` tools will appear in the tool list.

### Claude Code

Edit `./.mcp.json` (project) or `~/.claude.json` (user) — the same files `sutr connect claude-code`
writes:

```json
{
  "mcpServers": {
    "sutr": {
      "url": "https://app.sutr.sh/mcp",
      "headers": {
        "X-API-Key": "ap_your_key_here"
      }
    }
  }
}
```

Pair this with the [sutr-skills plugin](/connect/skills) so Claude Code knows the right way to use the gateway (discovery, approval polling, `additional_info` on every call).

### Cursor

Open Cursor settings, find the MCP section, and add:

```json
{
  "mcpServers": {
    "sutr": {
      "url": "https://app.sutr.sh/mcp",
      "headers": {
        "X-API-Key": "ap_your_key_here"
      }
    }
  }
}
```

### VS Code (Copilot / Continue)

Add to `.vscode/mcp.json` (or your client's equivalent MCP settings file):

```json
{
  "servers": {
    "sutr": {
      "url": "https://app.sutr.sh/mcp",
      "headers": {
        "X-API-Key": "ap_your_key_here"
      }
    }
  }
}
```


## Calling a tool, end to end

A typical sequence the agent runs:

1. `sutr__list_integration_tools(integration_id="github")` to see what's available.
2. `sutr__describe_tool(integration_id="github", tool_name="create_issue")` to get the input schema and the current approval mode.
3. `sutr__call_tool(...)` with the arguments and an `additional_info` string explaining intent.

### Auto-approved tool

The result comes back immediately, just like a normal tool call. Nothing for you to do.

### Approval-required tool

The agent gets a text response that contains an approval URL, e.g.:

```
This tool was marked by a human as needing approval. Share this URL with
a human and explain what you were trying to do:
https://app.sutr.sh/approve/a1b2c3d4-...

Then call sutr__await_approval(request_id="a1b2c3d4-...") to be
notified as soon as they decide.
```

A well-behaved agent will paste that URL into chat for you and immediately start polling `sutr__await_approval` in the same turn. You open the URL, see the exact tool name and parameters (and the agent's `additional_info` note), and approve or deny. The agent's poll returns either the actual tool result, a denial, or "still pending" — in which case it polls again.

You can also tick **Always approve** on the approval screen to auto-approve future calls to that tool.

### Denied tool

The call returns "This tool has been blocked and cannot be executed." A correctly behaved agent will stop and surface this to you rather than try to route around it.

## Helping coding agents do this right

If you're using Sutr from a coding agent (Claude Code, Cursor, etc.), install the [sutr-skills plugin](/connect/skills). The `sutr-mcp` skill teaches the agent the conventions that aren't obvious from the tool schemas alone:

- Always include a real `additional_info` sentence so approval reviewers have context.
- Make opaque IDs verifiable in `additional_info` (link to the Stripe customer, name the Gmail recipient, etc.).
- Start `await_approval` immediately after sharing an approval URL — don't wait for chat reply.
- Don't retry past a `deny`.

## Debugging latency

Self-hosted developers can profile the MCP gateway from `server/`:

```bash
uv run python scripts/mcp_speed_probe.py --api-key "$SUTR_API_KEY"
```

The probe reports cold MCP session setup, warm MCP calls, REST comparison endpoints, and local tool-cache state. To focus on one integration and compare Sutr against the upstream MCP server directly:

```bash
uv run python scripts/mcp_speed_probe.py \
  --api-key "$SUTR_API_KEY" \
  --integration-id github \
  --direct-upstream
```

The script is read-only by default. It only executes an integration tool if `--call-tool` is passed with `--integration-id`, `--tool-name`, and `--arguments`.

## MCP vs CLI

- **MCP** is the right choice when your AI client speaks MCP natively (Claude Desktop, Claude Code, Cursor, VS Code). The agent gets discovery, calling, and approval polling as first-class tools.
- The **[CLI](/connect/cli)** is for shell-based agents and scripts, or for ad-hoc calls from your terminal. It exposes the same gateway with the same approval behaviour.

Pick whichever your client supports best — both go through the same policy engine and audit log.

## See also

- [MCP Server reference](/mcp-server) — transport, auth, approval flow internals
- [Tool Approvals](/tool-approvals) — how approval policies work
- [sutr-skills](/connect/skills) — agent skills for using the gateway correctly
- [CLI](/connect/cli) — the command-line alternative
