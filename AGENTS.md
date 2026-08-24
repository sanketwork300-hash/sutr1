# Sutr

## What This Is

A universal tool gateway for AI agents. One place to manage every external capability an agent can call — remote MCP servers or plain REST APIs.

## Stack

| Concern | Choice |
|---------|--------|
| Server framework | FastAPI + uvicorn |
| CLI | TypeScript + Commander.js |
| ORM | SQLModel (SQLAlchemy 2.0 + Pydantic) |
| Migrations | Alembic |
| Database | SQLite (self-hosted) / PostgreSQL (SaaS) — swap via `DATABASE_URL` only |
| HTTP client (server) | httpx (async) |
| MCP protocol | `mcp` Python SDK |
| Server package manager | `uv` (always `uv run`, `uv add`, `uv sync` — never pip) |
| CLI package manager | `pnpm` (always pnpm — never npm or yarn) |
| Linting/formatting | ruff (server), tsc strict (cli) |
| Format on completion | **Always run `uv run ruff format` from `server/` before committing** |
| Testing (server) | pytest + pytest-asyncio + httpx |
| Testing (cli) | vitest |

## Architecture

```
Claude Desktop / VS Code / any MCP client
           │  MCP protocol (StreamableHTTP)
     ┌─────▼──────────────────────┐
     │     Sutr server        │  :4747  (Python / FastAPI)
     │  /mcp  → MCP endpoint       │  ← aggregates all integrations
     │  /api  → REST API           │  ← management
     │  /docs → Swagger UI         │  ← auto-generated
     └──────┬──────────────────────┘
            │ HTTP
     ┌──────▼──────┐      ┌──────────────┐
     │  CLI         │      │  UI (future) │
     │ (TypeScript  │      │              │
     │  Commander)  │      └──────────────┘
     └─────────────┘
```

## Key Architectural Decisions

- **CLI and server are fully independent.** Different packages (`server/`, `cli/`), different processes, communicate over HTTP only. The CLI never starts or manages the server process.
- **SQLite / PostgreSQL is purely a config swap.** Change `DATABASE_URL`, nothing else. No code differences between self-hosted (SQLite) and SaaS (PostgreSQL).
- **Integration definitions are Python classes.** Each bundled integration subclasses `RemoteMcpIntegration` or `CustomIntegration` with all fields as defaults. Instantiated with no args.

## Code Style & Formatting

After completing any work on the server, always run:

```bash
cd server && uv run ruff format
```

Do this before committing. No exceptions.

## Modularity (essential)

This is a hard rule, not a preference:

- `server/src/sutr/api/` — **one file per API resource.** Never combine resources into one file. If a resource grows beyond a single file, it becomes a directory with sub-files by concern.
- `cli/src/commands/` — **one file per command group.** Same rule.
- Prefer 5 focused 50-line files over 1 sprawling 250-line file.
- Never create a `utils.py` or `helpers.ts` dumping ground. If logic is shared, name it after what it actually does.

## Integration Types

| Type | Description | Example |
|------|-------------|---------|
| `remote_mcp` | MCP server hosted by vendor, connect via HTTP | PostHog, GitHub |
| `custom` | Plain REST API, proxy makes HTTP calls and presents as MCP tools | Stripe (via OpenAPI) |

A `custom` integration is always produced by the MCP builder from an OpenAPI specification —
the hand-rolled builder was removed, because deriving paths, parameters, and auth from a
document beats asking someone to retype them.

## Connected Accounts

OAuth grants sutr holds on a user's behalf, in `server/src/sutr/connections/`. Two uses, one
mechanism: **GitHub** authorizes reading specifications out of repositories, and **Google Cloud
/ Azure / AWS** authorize running generated MCP servers in the user's own account.

Rules that must hold:

- A connection is scoped to `(org, user, provider)`. It is a *personal* grant: another member
  must never be able to borrow it, and an API key cannot open one (it has no identity to revoke).
- Tokens live in `secret` rows through the configured secrets backend. The `provider_connection`
  row holds pointers, an expiry, and a display label — never a token.
- Everything that needs a token calls `connections.store.access_token()`, which refreshes when
  due. No caller decides whether a refresh is needed.
- The callback's `redirect_after` must be a relative path. An open redirect there turns an OAuth
  flow into a phishing hop.
- **AWS has no OAuth for its own service APIs.** The OAuth path for AWS is IAM Identity Center's
  `sso-oidc` device grant (RFC 8628); only that token is stored, and each operation exchanges it
  for short-lived role credentials.

## Deployment Providers

`server/src/sutr/deploy/` — one narrow interface, four implementations:

| id | Target | Pipeline |
|----|--------|----------|
| `docker` | The sutr host's own daemon | build image → run container on 127.0.0.1 |
| `gcp` | Cloud Run | GCS upload → Cloud Build → Artifact Registry → Cloud Run |
| `azure` | Container Apps | ACR source upload → ACR Task → Container Apps |
| `aws` | App Runner | S3 → CodeBuild → ECR → App Runner |

A provider declares its own `config_fields`, so the builder renders its form without knowing the
provider; adding a provider is a server change only. Credentials are never cached on the
deployment row — `deploy/credentials.py` resolves them fresh for every operation, because cloud
credentials are short-lived by design.

## Documentation (`docs/`)

The `docs/` directory is a [teeny](https://github.com/yakkomajuri/teeny) static site — it's the source of truth for anything a developer or agent needs to understand the system beyond the code itself. **Keep it updated** — if you add an endpoint, change a data model, or introduce a new integration type, update the relevant doc file in the same PR/commit.

Layout:
- `docs/pages/` — one Markdown file per page; filename becomes the URL slug (`pages/api.md` → `/api`).
- `docs/templates/default.html` — the HTML shell (sidebar + content) that wraps every page.
- `docs/static/` — CSS and other static assets.
- `docs/teeny.config.js`, `docs/package.json` — site config and `teeny-cli` dev dependency.

Current pages:
- `pages/api.md` — full REST API reference
- `pages/mcp-server.md` — `/mcp` aggregation endpoint
- `pages/tool-approvals.md` — tool approval/policy flow
- `pages/google-oauth-setup.md` — shared Google OAuth app setup

To add a page: drop a new `.md` file in `pages/` and add a link to the sidebar in `templates/default.html`. Run `pnpm dev` from `docs/` for a local hot-reload server, or `pnpm build` to produce static output in `public/`.

## Project Layout

```
sutr/
├── AGENTS.md           ← this file
├── CLAUDE.md           ← symlink to AGENTS.md
├── docs/               ← documentation (keep up to date — see above)
├── server/             ← Python / FastAPI
│   ├── pyproject.toml
│   └── src/sutr/
│       ├── api/        ← one file per resource
│       ├── models/     ← SQLModel tables
│       ├── mcp/        ← MCP server + proxy + upstream client
│       ├── integrations/
│       │   └── bundled/  ← one file per integration
│       ├── openapi/    ← OpenAPI spec → tool list generator (+ sources.py, packaging.py)
│       ├── connections/← connected accounts (OAuth grants for sources + deploy targets)
│       └── deploy/     ← deployment providers: docker, gcp, azure, aws/
└── cli/                ← TypeScript / Commander.js
    ├── package.json
    └── src/
        └── commands/   ← one file per command group
```
