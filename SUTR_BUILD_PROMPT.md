# Sutr Platform — Master Build Prompt

> Hand this entire document to the coding agent as its task prompt. It is written to keep the
> agent grounded in the real repository and the real references, and to prevent hallucinated
> APIs, invented file paths, or regressions to already-working functionality.

---

## 0. Non-negotiable ground rules (anti-hallucination contract)

You are working in an **existing, working codebase**, not a greenfield project. Obey these rules
at all times:

1. **Never invent a file, module, endpoint, model field, or CLI command.** Before referencing
   anything, verify it exists with Glob/Grep/Read. If it doesn't exist, say so and create it
   explicitly as new work.
2. **Never regenerate or rewrite working code.** Functionality marked ✅ IMPLEMENTED in §3 must
   be preserved byte-for-byte unless a task explicitly requires changing it. The 49 bundled
   integrations, the approval system, and the frontend are **AgentPort heritage — preserve them**.
3. **The reference projects in §4 are architectural references, not dependencies.** The server
   is Python/FastAPI; Keycloak is Java, OpenFGA is Go, Composio is TypeScript/Python. You study
   their *concepts, data models, API shapes, and flows* and reimplement them in the Sutr stack.
   Never `pip install keycloak` or vendor their code. The only references that are also usable
   libraries are noted in §4.
4. **Every claim of "done" must be backed by the verification gate** (§2). If tests fail, report
   the failure verbatim — never claim success.
5. **Do not guess external API behavior.** For MCP protocol behavior, consult the `mcp` Python
   SDK already in `server/pyproject.toml` and the MCP specification repo. For OpenAPI semantics,
   consult the OpenAPI 3.0/3.1 spec and the existing `server/src/sutr/openapi/` implementation.
6. **Database changes only via Alembic migrations** (`server/alembic/`), continuing the numbered
   sequence (currently at `0024`). Never edit an applied migration. SQLite and PostgreSQL must
   both work — `DATABASE_URL` is the only difference (hard architectural rule).
7. **When a spec in this document conflicts with the code, the code wins for existing behavior
   and this document wins for new behavior.** Log every such conflict in `SUTR_PROGRESS.md`
   instead of silently choosing.
8. **Update `SUTR_PROGRESS.md`** after every phase: what was built, decisions taken, known
   limitations, test count.

## 1. What Sutr is

Sutr is a universal tool gateway for AI agents: one place to manage every external capability an
agent can call — 49 bundled native integrations, custom remote MCP servers, and plain REST APIs
compiled from OpenAPI specs into MCP tools. It aggregates everything behind a single
authenticated `/mcp` endpoint (StreamableHTTP) with per-tool governance (approvals, policies),
multi-tenant identity (users, orgs, workspaces, RBAC), and a management REST API + React UI +
TypeScript CLI. Future phases add: generated standalone MCP servers, cloud deployment
(Kubernetes/Argo-style + Swaraj Cloud adapter), observability, metering/billing, and SDKs.

## 2. The repository — stack, layout, verification gate

Read `AGENTS.md` at repo root first; it is authoritative. Summary:

| Concern | Choice |
|---|---|
| Server | Python, FastAPI + uvicorn, port 4747 |
| ORM / migrations | SQLModel / Alembic |
| DB | SQLite (self-hosted) ↔ PostgreSQL (SaaS) via `DATABASE_URL` only |
| MCP | `mcp` Python SDK, StreamableHTTP at `/mcp` |
| CLI | TypeScript + Commander.js (`cli/`), package manager **pnpm only** |
| UI | React + Vite (`ui/`) |
| Server tooling | **uv only** (`uv run`, `uv add`, `uv sync` — never pip), ruff |

Layout (verified, do not invent siblings):

```
server/src/sutr/
  api/            ← one file per REST resource (hard rule; 26 routers exist)
  models/         ← SQLModel tables
  mcp/            ← server.py, client.py, oauth.py, oauth_provider.py, refresh.py, asgi.py,
                    management_tools.py, notifications.py
  integrations/bundled/  ← 49 integration classes, one file each
  openapi/        ← loader.py, normalizer.py, resolver.py, security.py, compiler.py,
                    limits.py, errors.py
  approvals/  billing/  secrets/  email/
  authz.py  security.py  token_auth.py  totp.py  upstream_safety.py  rate_limit.py
  maintenance.py  request_context.py  analytics.py
cli/src/commands/ ← auth.ts, integrations.ts, tools.ts, output.ts (one file per command group)
ui/src/           ← pages/, components/, api/, stores/, analytics/
docs/pages/       ← teeny static site; update in the same commit as any API/model change
```

**Verification gate — run after every phase, all must pass:**

```bash
cd server && uv run ruff format && uv run ruff check . && uv run pytest -q   # 599 green today
cd ui && pnpm exec tsc -b && pnpm exec vite build
cd cli && pnpm exec tsc --noEmit
```

Plus a live smoke test: boot the server; `/health` 200, `/api/config` 200, `/mcp` returns 401
with the RFC 9728 `WWW-Authenticate` header (`resource_name: "Sutr MCP"`).

## 3. Current implementation status (verified 2026-08-19 — do not rebuild ✅ items)

Status of the 51 functionality rows (§4 table numbering):

- ✅ **IMPLEMENTED** — #1 identity/2FA, #2 orgs/workspaces/RBAC, #3 the 49 integrations,
  #4 OAuth infra, #5 API-key auth, #6 registry, #7 `/mcp` gateway, #9 discovery,
  #10 execution, #14 custom remote MCP (SSRF-guarded), #15 OpenAPI import, #17 normalization,
  #18 cycle-safe `$ref`, #19 compiler IR, #22 collision detection, #24 security translation,
  #43 CLI, #46 REST API, #47 frontend, #49 admin.
- 🟡 **PARTIAL** — #8 (StreamableHTTP only; no stdio/SSE), #11 governance
  (approve-exact-forever + viewer `tools:execute` enforcement missing), #12 (logs/analytics
  exist; no OTel/Langfuse), #13 playground, #16 (spec-validator, no Spectral-style linting),
  #20/#21/#23/#25/#26/#27 (compiler core exists; REST wiring, UI wizard, HTTP runtime for
  compiled tools not wired), #41/#42, #44 (CLI package exists, unpublished), #48 (billing
  scaffolding, no event metering), #50, #51 (CI gates fly deploy; no full pipeline).
- ❌ **NOT BUILT** — #28–#40 (generated standalone servers, their testing/Docker packaging,
  all deployment: abstraction, lifecycle, monitoring, Swaraj Cloud adapter, runtime secrets
  injection, deployed-MCP security/governance), #29 extra transports, #45 SDKs.

Known limitations already documented (respect, don't "discover" them): datadog dual-token
storage is singular; `EnvVarAuth` is dead code; org deletion button inert; SQLite
`PRAGMA foreign_keys=ON` deferred pending delete-ordering review.

## 4. Functionality × reference map (authoritative)

For each row: implement the Sutr functionality; take **only** the listed concept from the listed
reference. Study the reference's docs/README/data model — do not import its code unless marked
"usable library".

| # | Functionality | Reference(s) | Take from it |
|---|---|---|---|
| 1 | Identity, signup/login, sessions, 2FA | [Keycloak](https://github.com/keycloak/keycloak) | User mgmt, authN, MFA, session model |
| 2 | Orgs/workspaces/RBAC | [OpenFGA](https://github.com/openfga/openfga), [Casbin](https://github.com/casbin/casbin) | Fine-grained authz patterns. **Decision on record:** Sutr uses a role matrix (`authz.py`), not ReBAC — flat resource graph. Only adopt relationship-tuple ideas if a phase explicitly upgrades authz. |
| 3 | 49 native integrations | **AgentPort (this repo)** | Preserve `integrations/bundled/` architecture and providers as-is |
| 4 | OAuth infrastructure | Keycloak, [Composio](https://github.com/ComposioHQ/composio) | OAuth clients, connected accounts, token lifecycle (exists: `auth_start.py`, `mcp/oauth*.py`, `mcp/refresh.py`) |
| 5 | API/token authentication | Composio | Unified auth-config + connected credentials (exists: `api_keys.py`, `token_auth.py`) |
| 6 | MCP server registry | [MCP spec](https://github.com/modelcontextprotocol/modelcontextprotocol) | Protocol contracts for servers/tools |
| 7 | Remote MCP gateway | Composio + MCP spec | Tool routing, remote MCP, agent access (exists: `mcp/server.py`, `mcp/client.py`) |
| 8 | MCP client compatibility | [MCP servers](https://github.com/modelcontextprotocol/servers) | Test against official clients/examples |
| 9 | Tool discovery | Composio | Search/list/filter tools & toolkits |
| 10 | Tool execution | Composio | Execution/session model |
| 11 | Approval/governance | **AgentPort** + OpenFGA | Preserve approval model (`approvals/`, `api/tool_approvals.py`); finish approve-exact-forever + viewer execute enforcement |
| 12 | Audit/observability | [OpenTelemetry Collector](https://github.com/open-telemetry/opentelemetry-collector), [Langfuse](https://github.com/langfuse/langfuse) | Structured traces/spans for tool calls. `opentelemetry-sdk` for Python **is a usable library**. |
| 13 | Integration playground | Composio | Search→test→execute workflow in UI |
| 14 | Custom remote MCP servers | MCP spec | Remote connection model (exists, SSRF-guarded via `upstream_safety.py`) |
| 15 | OpenAPI/Swagger import | [OpenAPI Generator](https://github.com/OpenAPITools/openapi-generator) | Parsing/generation ecosystem patterns |
| 16 | OpenAPI validation | [Spectral](https://github.com/stoplightio/spectral) | Actionable lint messages; Python equivalent: `openapi-spec-validator` (in use) + custom rules in `openapi/limits.py` |
| 17 | OpenAPI normalization | [swagger-parser](https://github.com/APIDevTools/swagger-parser) | Parse/dereference/normalize semantics (exists: `openapi/normalizer.py`) |
| 18 | Safe `$ref` resolution | swagger-parser | Cycle-safe resolution (exists: `openapi/resolver.py`, JSON-Pointer path tracking) |
| 19 | API→MCP IR | [CNOE openapi-mcp-codegen](https://github.com/cnoe-io/openapi-mcp-codegen) | OpenAPI → structured MCP server IR (exists: `openapi/compiler.py`) |
| 20 | Endpoint/tag filtering | [AWS Labs openapi-mcp-server](https://github.com/awslabs/mcp/tree/main/src/openapi-mcp-server) | Tag/operation selection UX |
| 21 | MCP tool generation | [openapi-mcp-generator](https://github.com/harsha-iiiv/openapi-mcp-generator) | Operation → MCP tool mapping |
| 22 | Tool-name collisions | openapi-mcp-generator + Sutr | Explicit collision detection (exists: post-snake_case collision tests) |
| 23 | LLM-oriented descriptions | AWS Labs openapi-mcp-server | Enriched descriptions, param info, response context |
| 24 | Security translation | openapi-mcp-generator | API key/Bearer/Basic/OAuth2 (exists: `openapi/security.py`) |
| 25 | HTTP execution runtime | [openapi-mcp](https://github.com/pvliesdonk/openapi-mcp) | Generic OpenAPI→HTTP→MCP runtime; wire compiled tools through the existing httpx proxy |
| 26 | JSON/form/multipart/binary | openapi-mcp-generator | Request serialization per `requestBody` media type |
| 27 | Server URL/variables | swagger-parser | Correct `servers` + variable substitution semantics |
| 28 | Dynamic + generated modes | openapi-mcp + CNOE codegen | Dynamic proxy (exists) + standalone generated server (new) |
| 29 | stdio/SSE/StreamableHTTP | openapi-mcp-generator | Multi-transport support in generated servers |
| 30 | Self-contained generated server | CNOE codegen | Complete Python package: client, config, docs |
| 31 | Generated-server testing | CNOE codegen | Emit eval/test scaffolding with each server |
| 32 | Docker packaging | CNOE codegen | Reproducible Dockerfile per generated server |
| 33 | Cloud deployment | [Argo CD](https://github.com/argoproj/argo-cd) | App lifecycle architecture |
| 34 | Deployment abstraction | [Kubernetes](https://github.com/kubernetes/kubernetes) + Argo CD | Provider-agnostic deployment interface |
| 35 | Swaraj Cloud | **Sutr-specific** | Implement as one adapter behind the §34 interface — obtain real Swaraj API docs from the user before coding; **never invent Swaraj endpoints** |
| 36 | Deployment lifecycle | Argo CD | deploy/sync/update/delete/status state machine |
| 37 | Deployment monitoring | Argo CD + [Prometheus](https://github.com/prometheus/prometheus) | Health/status/metrics |
| 38 | Runtime secrets | [External Secrets Operator](https://github.com/external-secrets/external-secrets) | Secret injection into workloads (platform secrets store exists: `secrets/`) |
| 39 | Deployed MCP security | Keycloak + OpenFGA | Identity + authz on deployed servers |
| 40 | Generated-tool governance | **AgentPort** + OpenFGA | Route generated tools through the existing approval system |
| 41 | Unified registry | Composio | One toolkit/tool abstraction over bundled + custom + compiled |
| 42 | Marketplace/discovery | Composio | App/tool discovery model |
| 43 | CLI | Composio | Auth, discovery, execution commands (exists: `sutr` bin, `ap` alias) |
| 44 | npm ecosystem | Composio | Publish `sutr-cli`; monorepo packaging |
| 45 | SDK | Composio | Python + TypeScript SDK architecture |
| 46 | REST API | Composio | Tool/auth/connected-account/toolkit API shapes (exists under `/api/*` — **do not rename routes**) |
| 47 | Frontend | **AgentPort** + Composio | Preserve existing UI; Composio is UX reference only |
| 48 | Billing/metering | [OpenMeter](https://github.com/openmeterio/openmeter) | Event-based usage metering (scaffolding exists: `billing/`) |
| 49 | Admin/control plane | Keycloak + **AgentPort** | Admin/user/org management (exists: `api/admin.py`) |
| 50 | Security | Keycloak + [OWASP CRS](https://github.com/coreruleset/coreruleset) | Security controls; WAF rules belong at the Caddy/edge layer |
| 51 | CI/CD & quality | Argo CD + CNOE codegen | Automated build/test/deploy (tests already gate fly deploy) |

## 5. Build order (remaining phases)

Work strictly in this order; each phase ends with the verification gate + a `SUTR_PROGRESS.md`
entry. Do not start a phase early because it "seems easy".

**Phase 4 — MCP hardening.** Reduce REST/MCP code duplication (shared pipeline); optional
per-call upstream connection reuse. Rows: 6–8, 10.

**Phase 5 — Governance completion.** Unify REST/MCP execution pipeline; approval
`approve-exact-forever`; enforce viewer `tools:execute` on all execution paths; audit trail
table + log redaction; decision-row locking. Rows: 11, 12 (audit part), 40.

**Phase 6 — OpenAPI compiler completion.** REST wiring for `openapi_projects` (compile,
preview, publish); UI import wizard (upload/URL → validate → filter by tag/operation → name
collisions surfaced → publish); Spectral-style actionable lint layer; LLM-oriented description
enrichment. Rows: 15–24 tails.

**Phase 7 — HTTP execution runtime.** Execute compiled tools through the gateway: httpx
runtime honoring `servers` variables, all `requestBody` media types (JSON/form/multipart/
binary), security schemes from `openapi/security.py`, per-integration rate limits, `/mcp`
exposure + approvals. Rows: 25–28 (dynamic mode).

**Phase 8 — Generated servers.** Emit a self-contained Python MCP server package per OpenAPI
project (CNOE-style): stdio + SSE + StreamableHTTP transports, tests, Dockerfile. Rows: 28–32.

**Phase 9 — Deployment.** `DeploymentProvider` interface (deploy/sync/update/delete/status);
Kubernetes adapter; **Swaraj Cloud adapter only after real API docs are provided**; health/
metrics; secret injection; deployed-server auth wired to Sutr identity. Rows: 33–39.

**Phase 10 — Observability & metering.** OTel tracing on every tool execution; usage events →
OpenMeter-style metering feeding `billing/`. Rows: 12, 48.

**Phase 11 — SDK & CLI.** TypeScript + Python SDKs mirroring `/api`; publish npm packages.
Rows: 43–45.

**Phase 12 — Frontend completion.** Playground (search→test→execute), marketplace/discovery
views, deployment dashboard, org Danger Zone (real org deletion). Rows: 13, 42, 47.

**Phase 13 — Security hardening.** OWASP-CRS-informed edge rules (Caddyfile), secrets-handling
review, dependency audit, authz fuzz tests. Row: 50.

**Phase 14 — E2E validation.** Full-journey live tests: signup → org → install integration →
OAuth connect → import OpenAPI → compile → execute → approve → deploy → observe → meter.

## 6. Definition of done (per phase)

1. Verification gate green (server tests ≥ previous count; UI + CLI builds clean).
2. New behavior covered by pytest/vitest tests, including failure paths.
3. `docs/pages/` updated in the same commit for any API/model change.
4. `SUTR_PROGRESS.md` entry: built / decisions / limitations / test count.
5. No renamed `/api/*` routes, no edited applied migrations, no reformatted unrelated files.
