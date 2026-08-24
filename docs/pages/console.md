---
title: The console
nav_title: Console
---
# The console

The Swaraj Sutr console is a control plane, not a dashboard. It is organised by what you are doing — building capabilities, operating them, governing them, developing against them — rather than by which table a record lives in.

The public site is at `/`. Everything below is the authenticated console, which lives under `/app`.

```
/                     Public site
/app                  Overview
/login /signup        Authentication
/docs                 API reference (OpenAPI, served by the server itself)
```

Links from before the `/app` move — OAuth returns, billing redirects, invitation mails, older bookmarks — still resolve. The pre-move paths (integrations, deployments, approvals, activity, usage, connect, playground, settings and admin, with their sub-paths) redirect to their `/app` equivalents, preserving query strings and fragments.

---

## Navigation

| Group | Page | Route | What it shows |
|-------|------|-------|---------------|
| | Overview | `/app` | Infrastructure counts, recent executions, the request path |
| **Build** | APIs | `/app/apis` | Imported OpenAPI specifications and their normalized representation |
| | MCP Servers | `/app/mcp-servers` | Generated servers you run, remote servers you connect out to, and this instance's aggregate endpoint |
| | Integrations | `/app/integrations` | The provider catalog: authentication, tool count, risk, connection state, last use |
| | Tools | `/app/tools` | Every governed capability, its schema, its policy, and its recent executions |
| **Operate** | Playground | `/app/playground` | Call a tool by hand through the same governed path an agent uses |
| | Approvals | `/app/approvals` | The queue of calls waiting on a human decision |
| | Activity | `/app/activity` | Execution timeline and the configuration audit trail |
| | Deployments | `/app/deployments` | Generated MCP servers, their provider, status and endpoint |
| **Govern** | Policies | `/app/policies` | The approval window, and every tool that departs from auto-approve |
| | Access | `/app/access` | Members, roles, invitations and workspaces |
| | Credentials | `/app/credentials` | Connected accounts and integration credentials — never secret values |
| **Develop** | API Keys | `/app/api-keys` | Keys your services present to this instance |
| | Connect | `/app/connect` | Client setup for Claude Code, Cursor, Codex and others |
| | SDK | `/app/sdk` | Python and TypeScript quick starts, including the approval flow |
| | CLI | `/app/cli` | The command surface, group by group |
| | Usage | `/app/usage` | Metered executions, latency, outcomes and providers |
| | Settings | `/app/settings` | Account, security and appearance |

---

## Command palette

`Cmd/Ctrl + K` anywhere in the console. It searches the workspace inventory — tools, integrations, deployments and API projects — alongside the standing actions (import a specification, create an MCP server, create an API key). Every result navigates; nothing in it is decorative.

The inventory is fetched the first time the palette is opened, never at page load.

---

## Environment indicator

The top bar reports which instance you are looking at, derived from the host the console is served from: `LOCAL` for localhost, `STAGING` for a host whose name contains `staging`, `stage`, `dev`, `preview` or `test`, `PRODUCTION` otherwise. There is no environment field on the API, so nothing more is claimed.

---

## Workspace switcher

The switcher shows the organisation and its workspaces, and remembers which workspace you selected in this browser. The REST surface is organisation-scoped, so selecting a workspace does not filter what the API returns — the switcher says so rather than implying isolation the server does not yet enforce. Workspaces are managed under **Govern → Access**.

---

## Status vocabulary

One word per state, everywhere:

```
CONNECTED   DISCONNECTED   NOT CONFIGURED   NEEDS REAUTHORIZATION
QUEUED      BUILDING       RUNNING          STOPPED         FAILED
APPROVAL REQUIRED   APPROVED   AUTO APPROVED   CONSUMED   DENIED   EXPIRED
EXECUTED    ERROR          PENDING
AUTO APPROVE   ASK   DENY
```

A deployment reads `RUNNING`, not `HEALTHY`: a running container is what the provider actually confirmed. Anything the console has not loaded shows a skeleton; anything that is genuinely zero shows zero.

---

## Appearance

The console is dark-first — the dark palette is the reference design and the default for a new visitor. The light theme is a full alternative, not a fallback, and the choice is stored per browser. `prefers-reduced-motion` is respected everywhere: diagrams still render, they simply stop moving.
