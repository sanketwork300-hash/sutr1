---
title: Sutr CLI
nav_title: CLI
---

# Sutr CLI

```sh
npm install -g sutr-cli
```

`ap` is the command-line interface to Sutr. It is the second of the two ways into the gateway, with the other being our [MCP server](/connect/mcp).

Both surface the same integrations, the same tools, and run through the same approval policies. Pick whichever fits your agent best (some can only run one or the other).


## Installation

The CLI ships as the `sutr-cli` npm package and exposes two binaries: `sutr` and the short alias `ap`.

```sh
npm install -g sutr-cli
```


## Configuration

Configuration lives at `~/.config/sutr/config.json` and is managed by the CLI so you shouldn't have to manually edit it. It stores the server URL, the chosen auth mode (currently `api_key`), the API key, and your default output format.

The default server URL is `https://app.sutr.sh`. Override it with the `SUTR_URL` environment variable, or persist a different instance with:

```sh
ap auth set-instance-url https://ap.example.com
```

Switching instances clears any stored credentials, so you'll need to re-run `ap auth login`.

## Skills

You should really install the [Sutr Skills](/connect/skills) for your agent to use the Sutr CLI most efficiently.

## Authentication

The CLI authenticates with an API key issued from the Sutr UI (Develop → API Keys):

```sh
ap auth login --api-key ap_...
```

> Browser OAuth is temporarily unavailable: MCP-audience OAuth tokens no longer unlock the REST API (security audit finding 09), and the CLI does not yet have a REST-scoped OAuth issuer. API keys are the supported CLI credential until that lands.

### Status and logout

```sh
ap auth status   # show the configured URL, auth mode, masked tokens, and your email
ap auth logout   # wipe stored credentials
```

---

## Connecting an agent

`sutr connect` is the one command that wires an AI agent into the gateway. Run it with no
arguments to verify the connection and print the setup, or name an agent to have the CLI write
the MCP entry into that agent's own configuration file.

```sh
sutr connect                    # verify the connection, print the endpoint and the config shape
sutr connect claude-code        # write the MCP entry into ./.mcp.json
sutr connect --list             # agents the CLI can configure, and where each keeps its config
```

```text
$ sutr connect claude-code

  ╭────────────────────────────────────╮
  │  Sutr                              │
  │  Secure tool access for AI agents  │
  ╰────────────────────────────────────╯

  ◆ Connecting Claude Code to Sutr...

  ✓ Checking configuration       https://app.sutr.sh
  ✓ Authenticating session       organization credential ap_1a2b...9z8y
  ✓ Verifying the MCP endpoint   https://app.sutr.sh/mcp
  ✓ Loading available tools      50 tools across 2 integrations
  ✓ Reading access policies      12 auto-approve · 37 need approval · 1 denied
  ✓ Writing agent configuration  /home/you/project/.mcp.json

  ✦ Sutr is ready.

  Your agent can reach 50 tools across 2 integrations,
  with your approval policies enforced on every call.

  Credential: ap_1a2b...9z8y
  MCP endpoint: https://app.sutr.sh/mcp
  Agent: Claude Code (MCP)
  Config file: /home/you/project/.mcp.json (created)

  Restart Claude Code (or run /mcp) to pick up the server.

  Next step:
  Install Sutr Skills so your agent knows how to use Sutr:

  npx skills add sutr-dev/sutr-skills
```

Every line above is the result of an operation that actually ran: the configuration is read, the
stored credential is presented to the server, the `/mcp` route is probed, the tool list is fetched,
and the policy split is computed from that list. Nothing is announced before it has happened.

### What it configures

| Agent | Scope | File |
|-------|-------|------|
| `claude-code` | `project` (default), `user` | `./.mcp.json`, `~/.claude.json` |
| `claude-desktop` | `user` | `claude_desktop_config.json` (via the `mcp-remote` bridge) |
| `cursor` | `user` (default), `project` | `~/.cursor/mcp.json`, `./.cursor/mcp.json` |
| `vscode` | `project` | `./.vscode/mcp.json` |
| `codex` | `user` | `~/.codex/config.toml` |

The CLI merges a single `sutr` entry into whatever is already in the file, keeps the previous
contents next to it as `<file>.sutr-backup`, and restricts the file to `0600` when the entry
carries your API key. If it cannot parse the file, or Codex already has an `[mcp_servers.sutr]`
table, it prints the entry to add and changes nothing.

Before writing, it shows the exact change — with the key masked — and asks. Pass `--yes` to skip
the prompt, or `--dry-run` to see the change without applying it. In a non-interactive shell
(a pipe, CI, a container) it refuses to modify a file unless `--yes` was given.

### Flags

| Flag | Purpose |
|------|---------|
| `--list` | List the agents the CLI can configure. Works with `-o json`. |
| `--scope <user\|project>` | Where to write the entry. Defaults per agent, as above. |
| `--no-api-key` | Leave the key out of the config; the client authenticates itself (OAuth). |
| `--no-animation` | Static output: no spinner, no in-place redrawing. |
| `--quiet` | Only warnings, failures and machine-readable output. |
| `-y, --yes` | Apply the configuration change without asking. |
| `--dry-run` | Show the change, write nothing. |
| `--timeout <seconds>` | Per-request deadline. Default `20`. |
| `-o, --output <format>` | `human`, `json` or `toon`. |

### Non-interactive behaviour

The terminal experience adapts to where the output is going; scripts and CI see plain text.

- Animation is on only when the output stream is a TTY. A pipe, a file, a CI job
  (`CI=true`, `GITHUB_ACTIONS`) or `SUTR_NO_ANIMATION=1` all get static lines instead.
- `NO_COLOR` (any non-empty value) removes every ANSI escape; `FORCE_COLOR` puts it back.
- Terminals that cannot render box-drawing glyphs — a bare Windows console, `LANG=C` — get an
  ASCII fallback. `SUTR_ASCII=1` forces it.
- With `-o json` or `-o toon`, stdout carries only the result document and all progress moves to
  stderr, so `sutr connect -o json | jq` is safe. On failure, stdout carries
  `{"error": {"title": …, "reason": …, "hint": …}}` and the exit code is `1`.
- Exit codes: `0` success, `1` failure, `130` interrupted (ctrl-c) or declined at the prompt.

### When it fails

Failures name the step that failed, the underlying reason, and the next command to run:

```text
  ✓ Checking configuration       https://app.sutr.sh
  ✗ Authenticating session

  ✗ Authentication failed

  Reason: HTTP 401 — Invalid API key

  Next step:
  Create an API key in the Sutr UI under Develop → API Keys, then run:
  sutr auth login --api-key ap_...
```

| Symptom | What to do |
|---------|-----------|
| `Not authenticated` / `HTTP 401` | The stored key is missing or rejected. `sutr auth login --api-key ap_...`. |
| `Could not connect to Sutr` | The instance is unreachable. Check `sutr auth status`, then the URL and your network. |
| `Timed out` | The server did not answer within `--timeout` seconds. Raise it, or check the instance. |
| `MCP endpoint not found` | The URL does not point at a Sutr instance, or it predates the MCP gateway. |
| `Invalid configuration` | The stored URL is not an `http(s)` URL. `sutr auth set-instance-url https://app.sutr.sh`. |
| `Refusing to change a configuration file unattended` | Re-run with `--yes`, or with `--dry-run` first. |
| `Could not read the agent configuration` | The agent's config file is not valid JSON. Fix it, or move it aside. |

### Three separate things

- **Connectivity** — `sutr connect` points your agent at the `/mcp` endpoint. That is this command.
- **Authentication and policy** — your API key identifies the organisation, and the server enforces
  the per-tool approval policy on every call. Nothing about that lives in the agent's config.
- **[Sutr Skills](/connect/skills)** — a separate repository, installed by you with
  `npx skills add sutr-dev/sutr-skills`, that teaches the agent the conventions of the gateway.
  Sutr works without them; the CLI only ever shows the command, and never runs it.

---

## Output formats

Every command takes `-o <format>` where `<format>` is one of:

- `human` — pretty tables and key/value pairs (default, not safe to parse).
- `json` — stable, machine-readable JSON.
- `toon` — TOON-encoded output for token-efficient consumption by LLMs.

To change the default for every command:

```sh
ap output json
```

## Command reference

The groups are `auth`, `connect`, `connections`, `deploy`, `integrations`, `marketplace`,
`openapi`, `output`, `quota`, `tools` and `usage`. The most used ones are documented below.

### `ap auth`

Manage CLI credentials and the target instance.

| Command | Purpose |
|---------|---------|
| `ap auth login --api-key <key>` | Store an API key issued from the UI as the CLI credential. |
| `ap auth logout` | Remove stored credentials. |
| `ap auth status` | Show the configured URL, auth mode, masked tokens, and your account email. |
| `ap auth set-instance-url <url>` | Point the CLI at a different Sutr instance. Clears credentials. |

Example:

```sh
ap auth status -o json
```

### `ap integrations`

List, install, and remove integrations.

| Command | Purpose |
|---------|---------|
| `ap integrations list` | List integrations. Defaults to all; use `--installed` or `--available` to filter. |
| `ap integrations add <integration>` | Install an integration. Pick auth with `--auth token\|oauth`; pass `--token <token>` for token auth. |
| `ap integrations remove <integration>` | Uninstall an integration. |

`ap integrations add` is interactive: for `--auth token` it will prompt for the secret if you don't pass `--token`, and for `--auth oauth` it opens the upstream OAuth URL in a browser and polls for completion (timeout configurable with `--timeout <seconds>`, default `180`). Use `--no-open` in headless environments to print the URL instead of opening it.

If the integration declares only one auth method, you can omit `--auth`.

Examples:

```sh
ap integrations list --installed -o json
ap integrations add github --auth oauth
ap integrations add resend --auth token --token re_...
ap integrations remove linear
```

### `ap tools`

List, describe, and call tools across installed integrations.

| Command | Purpose |
|---------|---------|
| `ap tools list` | List all tools. Use `--integration <id>` to scope to one integration. |
| `ap tools describe --integration <id> --tool <name>` | Show the tool's description, execution mode, and full `inputSchema`. |
| `ap tools call --integration <id> --tool <name> --args <json>` | Execute a tool. Pass `--info <text>` to record intent for reviewers, or `--wait` to keep waiting through approval gates and retry automatically. |
| `ap tools await-approval --request-id <id>` | Wait for an approval decision. You can pass `--approval-url <url>` instead of `--request-id` if that's what you have. |

Examples:

```sh
ap tools list --integration github -o json
ap tools describe --integration github --tool create_issue -o json
ap tools call --integration github --tool create_issue \
  --args '{"repo":"sutr-dev/sutr","title":"docs: typo"}' \
  --info "Filing the typo Sam noticed in the README." \
  -o json
ap tools call --integration github --tool create_issue \
  --args '{"repo":"sutr-dev/sutr","title":"docs: typo"}' \
  --info "Filing the typo Sam noticed in the README." \
  --wait --wait-timeout 600 \
  -o json
ap tools await-approval \
  --approval-url https://app.sutr.sh/approve/550e8400-e29b-41d4-a716-446655440000 \
  -o json
```

### `ap output`

Set the persisted default output format.

```sh
ap output json    # subsequent commands default to JSON
ap output human   # back to the pretty default
ap output toon
```


### Exit codes

`ap tools call` distinguishes outcomes via exit code:

| Exit code | Meaning | What to do |
|-----------|---------|------------|
| `0` | Success. Tool result on stdout. | Parse and use. |
| `1` | Error, denial, or a non-awaitable approval state. Message on stderr. | Stop. Don't retry, don't try a different tool to reach the same outcome. |
| `2` | Approval required, or still pending after `--wait-timeout`. Stderr contains `Approval required: <url>` or `Approval still pending: <url>`. | Surface the URL to the human. Either use `ap tools call --wait`, or wait separately with `ap tools await-approval` and then retry the **identical** command. |

See [Tool Approvals](/tool-approvals) for how the approval flow works on the server side.

`ap tools await-approval` also uses exit codes:

| Exit code | Meaning | What to do |
|-----------|---------|------------|
| `0` | Approved. Stdout includes `status: "approved"`. | Retry the original `ap tools call` if you did not use `--wait`. |
| `1` | Denied, expired, consumed, or invalid request. | Stop and surface the status. |
| `2` | Still pending at the requested timeout. | Keep waiting or ask the human to decide. |

### Quick reference

| Goal | Command |
|------|---------|
| Am I logged in? | `ap auth status -o json` |
| Connect my agent | `sutr connect <agent>` (`sutr connect --list` for the agents) |
| What's installed? | `ap integrations list --installed -o json` |
| What's available? | `ap integrations list --available -o json` |
| What tools does X expose? | `ap tools list --integration <id> -o json` |
| What does tool Y take? | `ap tools describe --integration <id> --tool <name> -o json` |
| Run tool Y | `ap tools call --integration <id> --tool <name> --args '<json>' --info '<why>' -o json` |
| Run tool Y and wait through approvals | `ap tools call --integration <id> --tool <name> --args '<json>' --info '<why>' --wait -o json` |
| Wait on an approval gate | `ap tools await-approval --approval-url '<url>' -o json` |

---

## Related

- [MCP Aggregation Endpoint](/connect/mcp) — the other way into the gateway.
- [Skills](/connect/skills) — drop-in skills that teach coding agents how to drive Sutr.
- [Tool Approvals](/tool-approvals) — how `require_approval` calls are routed to a human.
