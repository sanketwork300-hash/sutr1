---
title: Claude Code
nav_title: Claude Code
---

# Claude Code

This page walks through wiring [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Anthropic's CLI) to Sutr over MCP, installing the `sutr-skills` plugin so the agent uses the gateway correctly, and configuring auto-approval so Claude Code doesn't prompt you on top of Sutr's own approval flow. For the broader MCP picture see [Connecting via MCP](/connect/mcp).

## Prerequisites

- Claude Code installed and on your `PATH` (`claude --version` works).
- An Sutr account — either the cloud deployment at `https://app.sutr.sh` or a self-hosted instance (default `http://localhost:4747`).

In the steps below, replace `<mcpUrl>` with whichever applies:

| Deployment | MCP URL |
|------------|---------|
| Cloud | `https://app.sutr.sh/mcp` |
| Self-hosted | `http://localhost:4747/mcp` |

## Step 1: Add the MCP server

Run the following command to connect to the Sutr MCP server:

```bash
# change app.sutr.sh for your domain if self-hosting
claude mcp add sutr -s user --transport http https://app.sutr.sh/mcp
```

Or, if you have the [Sutr CLI](/connect/cli) installed and authenticated, let it verify the
connection and write `./.mcp.json` for you:

```bash
sutr connect claude-code
```


## Step 2: Install the Sutr skills

```bash
npx skills add sutr-dev/sutr-skills
```

This installs the [`sutr-skills`](/connect/skills) plugin, which teaches Claude Code the conventions of the gateway, such as how the `sutr__*` tools fit together, how to surface approval URLs, and how to long-poll `await_approval` in the same turn.

## Step 3: Auto-approve Sutr tool calls

Sutr gates approvals itself: every tool call routes through your configured policy and, when needed, asks you for explicit approval via the Sutr UI. There's no benefit to Claude Code also prompting you before it forwards the call as you'd end up confirming the same action twice, so you can allow Sutr MCP tool calls by default. If you're running Claude Code in a sandboxed enviroment, Sutr let's you connect third-party integrations and still run in "bypass permissions" mode.

Add the following to `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["mcp__sutr__*"]
  }
}
```

## Step 4: Authenticate

Start a Claude Code session and run:

```
/mcp
```

Pick `sutr` from the list and follow the OAuth flow. Once it completes, Claude Code will list the `sutr__*` tools alongside any others you have configured.

## Verifying it works

In a Claude Code session, ask:

> List my Sutr integrations.

Claude Code should call `sutr__list_installed_integrations` and report back. If you have nothing installed yet, ask it to install one — for example, "install the GitHub integration on Sutr" and watch the approval flow play out.

## Troubleshooting

- **`sutr` doesn't appear in `/mcp`.** Re-run `claude mcp list` to confirm the server is registered. If it's missing, repeat Step 1 — make sure you used `-s user` and the URL ends in `/mcp`.
- **`401 Unauthorized` from the MCP endpoint.** The OAuth session has expired or never completed. Run `/mcp` again and walk through the auth flow.
- **An approval URL isn't shown in chat.** This usually means the `sutr-skills` plugin isn't installed or didn't load. Re-run `npx skills add sutr-dev/sutr-skills` from the project directory and restart Claude Code.

## See also

- [Connecting via MCP](/connect/mcp) — endpoint, transport, and other clients.
- [Agent Skills](/connect/skills) — what the `sutr-skills` plugin does and how to update it.
- [Tool Approvals](/tool-approvals) — how Sutr decides when to prompt you.
