---
title: Claude Desktop
nav_title: Claude Desktop
---

# Claude Desktop

This page walks through connecting the Claude Desktop app (macOS / Windows) to Sutr over MCP, so Claude can install integrations and call tools through your gateway with your approval policies enforced.

Sutr exposes a single Streamable HTTP MCP endpoint at `https://app.sutr.sh/mcp` (or your self-hosted URL). Claude Desktop has two ways to talk to it: a built-in **Custom Connectors** UI on paid plans, or the `mcp-remote` stdio bridge on any plan. Both are documented below.

## Prerequisites

- **Claude Desktop** installed — download from [claude.ai/download](https://claude.ai/download).
- **An Sutr API key** — generate one from the **Connect** page in the Sutr UI ([app.sutr.sh](https://app.sutr.sh) → Connect). Keys begin with `ap_`.
- **Node.js 18+** on your machine — only required if you use Approach B (the `mcp-remote` bridge).

## Approach A: Custom Connectors (paid plans)

Anthropic's Custom Connectors UI lets Claude Desktop talk to a remote MCP server natively, with no local bridge. It is currently available on Claude **Pro**, **Max**, **Team**, and **Enterprise** plans. (If you don't see the option, you're likely on the Free tier — use Approach B instead.)

1. Open Claude Desktop and go to **Settings → Connectors**.
2. Scroll to the bottom and click **Add custom connector**.
3. Fill in the form:
   - **Name:** `Sutr`
   - **Remote MCP server URL:** `https://app.sutr.sh/mcp`
4. Click **Add**, then open the connector you just created and configure authentication. Sutr accepts OAuth or an `X-API-Key` header. If the UI offers OAuth, use it. Otherwise add a custom header:
   - **Header name:** `X-API-Key`
   - **Header value:** your `ap_...` key
5. Save. The connector should turn on, and `sutr__*` tools will be available in any new chat.

If you're on a self-hosted Sutr, swap in `https://<your_domain>/mcp`.

## Approach B: `mcp-remote` bridge (any plan)

Claude Desktop's `claude_desktop_config.json` only natively supports **stdio** MCP servers. To connect to Sutr's HTTP endpoint from any plan (including Free), use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) npm package as a stdio-to-HTTP bridge.

1. Open `claude_desktop_config.json`. The path is:
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

   If the file doesn't exist, create it with `{}` as the contents.

2. Add an `sutr` entry under `mcpServers`:

   ```json
   {
     "mcpServers": {
       "sutr": {
         "command": "npx",
         "args": [
           "-y",
           "mcp-remote",
           "https://app.sutr.sh/mcp",
           "--header",
           "X-API-Key:${SUTR_API_KEY}"
         ],
         "env": {
           "SUTR_API_KEY": "ap_your_key_here"
         }
       }
     }
   }
   ```

   Replace `ap_your_key_here` with your real key. If you're self-hosting, replace the URL.

3. Save and **fully quit Claude Desktop** (Cmd-Q on macOS, not just close the window), then reopen it. Claude Desktop only reads the config file at launch.

`mcp-remote` will be downloaded by `npx` on first run. It keeps a small token/session cache under `~/.mcp-auth/`.

## Verifying it works

1. Open a new chat in Claude Desktop.
2. Click the tool / search-and-tools icon next to the message input.
3. You should see an `sutr` server with the `sutr__*` meta-tools listed: `list_installed_integrations`, `list_integration_tools`, `describe_tool`, `call_tool`, `await_approval`, and the install helpers.
4. Ask Claude something like "list my installed Sutr integrations" and it should call `sutr__list_installed_integrations` and return the result.

## A note on agent skills

The [`sutr-skills`](/connect/skills) plugin teaches coding agents conventions like always passing `additional_info` and starting `await_approval` in the same turn as a gated call. It only installs into agents with a plugin directory (Claude Code, Cursor, OpenAI Codex) — Claude Desktop has no equivalent install target, so there is no skill step here. Claude Desktop will still use Sutr correctly; you may occasionally want to remind it to include a one-line reason when calling approval-gated tools.

## Troubleshooting

**The `sutr` server doesn't appear in the tool list.**
Quit Claude Desktop completely and relaunch — the config is only re-read at startup. On macOS, "close window" is not the same as "quit". If you used Approach B, also confirm the JSON parses (a missing comma will silently disable the whole `mcpServers` block).

**"Authentication failed" or 401 errors.**
Double-check the `X-API-Key` value. In the JSON config, the bridge passes the header as `X-API-Key:<value>` — make sure the env var actually expanded. Generate a fresh key from the Connect page if in doubt.

**`npx` can't find `mcp-remote` or hangs on first launch.**
The first launch downloads `mcp-remote`. Run `npx -y mcp-remote --help` once in a terminal to pre-warm the cache and confirm Node is installed and on `PATH`. Claude Desktop on macOS launches with a minimal `PATH` — if `npx` lives somewhere unusual (e.g. via `nvm`), point `command` at the absolute path to `npx`.

**Tool calls hang for ~5 minutes then fail.**
That's the approval timeout. Open the URL Claude shared, approve the request, and the call will resume. See [Tool Approvals](/tool-approvals) for details on the policy flow.

## See also

- [Connecting via MCP](/connect/mcp) — the gateway model and tool surface.
- [Tool Approvals](/tool-approvals) — how approval policies work end-to-end.
- [MCP Server reference](/mcp-server) — transport, auth, and approval internals.
