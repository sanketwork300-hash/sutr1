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
   sequence (head is `0028_security_hardening.py`). Never edit an applied migration. SQLite and
   PostgreSQL must both work — `DATABASE_URL` is the only difference (hard architectural rule).
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

Also present, added after the original layout: `server/src/sutr/services/` (the shared service
layer: `tool_pipeline.py`, `audit.py`, `metering.py`, `redaction.py`, `tool_catalog.py`,
`deployments.py`), `server/src/sutr/observability/` (`metrics.py`, `tracing.py`),
`server/src/sutr/deploy/` (`base.py`, `registry.py`, `credentials.py`, `cloud_http.py`,
`docker_provider.py`, `gcp_provider.py`, `azure_provider.py`, `aws/`),
`server/src/sutr/connections/` (connected accounts: `providers.py`, `flow.py`, `device.py`,
`store.py`, `targets.py`), `server/src/sutr/openapi/sources.py` + `packaging.py`, and
`sdk/python/` + `sdk/typescript/`.

**Verification gate — run after every phase, all must pass:**

```bash
cd server && uv run ruff format && uv run ruff check . && uv run pytest -q   # 732 green today
cd ui && pnpm exec tsc -b && pnpm exec vite build
cd cli && pnpm exec tsc --noEmit
```

Plus a live smoke test: boot the server; `/health` 200, `/api/config` 200, `/mcp` returns 401
with the RFC 9728 `WWW-Authenticate` header (`resource_name: "Sutr MCP"`).

`ui/` currently carries **15 pre-existing eslint errors** in inherited files (react-refresh
export rules, `setState`-in-effect, useless escapes). They are not a gate. Do not "fix" them as
a side quest, and do not add new ones: `pnpm exec eslint <files you touched>` must be clean.

## 3. Current implementation status (re-audited against the code 2026-08-20)

Every row below was verified by reading the named file. **Do not rebuild ✅ rows.** For 🟡 rows,
build only the named missing piece. Row numbers match the §4 table.

**✅ IMPLEMENTED (33 rows) — evidence:**

| # | Where it lives |
|---|---|
| 1 | `api/user_auth.py` (login, lockout, rate limit), `api/totp.py` (real TOTP + recovery codes), `api/second_factor.py` |
| 2 | `authz.py` — 5-role matrix × 11 permissions; `api/orgs.py` (members, invitations, last-owner protection), `api/workspaces.py` |
| 3 | `integrations/bundled/` — 48 integration modules |
| 4 | `api/auth.py` (start/callback/token exchange), `mcp/oauth.py` (expiry + refresh), `mcp/oauth_provider.py` (Sutr as an OAuth provider) |
| 5 | `api/api_keys.py` (hashed `ap_` keys), consumed by `dependencies.py` and `mcp/asgi.py` |
| 6 | `api/custom_mcp.py` + `integrations/registry.py`, exposed via `mcp/management_tools.py` |
| 7 | `main.py` mounts `/mcp`; `mcp/server.py` `StreamableHTTPSessionManager`; auth in `mcp/asgi.py` |
| 9 | `api/tools.py` (list/describe) and `mcp/management_tools.py` (search across integrations) |
| 10 | `services/tool_pipeline.py` — the single canonical pipeline shared by REST and MCP, incl. OAuth refresh-retry, metering, tracing, logging |
| 11 | `approvals/policy.py` (allow/deny/require_approval), `approvals/requests.py` (approve-once, approve-exact-forever, allow-tool-forever), viewer `tools:execute` enforced, audit trail + `services/redaction.py` |
| 13 | `ui/src/pages/PlaygroundPage.tsx` — selector → schema-driven form → execute, with live approval long-poll |
| 14 | `api/custom_mcp.py` guarded by `upstream_safety.py` (DNS-resolved loopback/private/link-local blocks, no redirects, no embedded credentials) |
| 18 | `openapi/resolver.py` — DFS with `on_path` cycle placeholders, external `$ref` refused, depth/node budgets |
| 19 | `openapi/normalizer.py` — `ApiDefinition`/`Operation`/`Server`/`SecurityScheme` IR, persisted as `ir_json` |
| 20 | `openapi/compiler.py` `filter_operations` (tags/paths/operationIds/deprecated) + selection UI in `McpBuilderPage.tsx` |
| 21 | `openapi/compiler.py` `compile_definition` |
| 22 | `openapi/compiler.py` `assign_tool_names` — deterministic 3-step naming, fails loudly rather than duplicating; `renamed_from` surfaced |
| 23 | `openapi/compiler.py` `_tool_description` + per-param descriptions into the input schema (spec-sourced only, nothing invented) |
| 25 | `api_client.py` `dispatch_api_tool` — real httpx call, SSRF re-validated at dispatch, response truncation |
| 27 | `openapi/normalizer.py` `substitute_server_url` (enum-validated, raises on unresolved vars) + server picker UI |
| 30 | `openapi/packaging.py` — 9-file self-contained package, zero Sutr dependency |
| 31 | `openapi/packaging.py` `TEST_PY` — offline pytest suite emitted per package |
| 32 | `openapi/packaging.py` `DOCKERFILE` — per-package image |
| 34 | `deploy/base.py` `DeploymentProvider` ABC (available/deploy/status/start/stop/remove/logs, each taking a `ProviderTarget` of credentials + placement) + `deploy/registry.py`; providers declare their own `config_fields` so the builder renders a provider's form without knowing the provider |
| 33 | Cloud deployment — `deploy/gcp_provider.py` (Cloud Build → Artifact Registry → Cloud Run), `deploy/azure_provider.py` (ACR Tasks → Container Apps), `deploy/aws/provider.py` (S3 → CodeBuild → ECR → App Runner). All authorized by a connected account; credentials resolved fresh per operation in `deploy/credentials.py` |
| 38 | `services/deployments.py` → `deploy/docker_provider.py`: KMS-backed secrets injected as env vars at deploy time, never baked into the image |
| 41 | `integrations/registry.py` merges bundled + custom MCP + compiled OpenAPI; `services/tool_catalog.py` unifies discovery/caching |
| 43 | `cli/src/index.ts` — 8 command groups: auth, connections, deploy, integrations, openapi, tools, output, usage |
| 44 | `sutr-cli` v1.1.1 **is published on npm** and matches the local version (manual publish; no CI job) |
| 46 | 29 routers under `api/`, 30 `include_router` calls in `main.py` |
| 47 | 24 pages under `ui/src/pages/` |
| — | Connected accounts (`connections/`, `api/connections.py`, migration `0029`): OAuth authorization-code + PKCE for GitHub/GCP/Azure, RFC 8628 device grant for AWS IAM Identity Center. Tokens held as `secret` rows through the configured backend; connections are per-user and cannot be borrowed by another member |
| 49 | `api/admin.py` (instance settings, waitlist, users, impersonation) + `ui/src/pages/AdminPage.tsx` |
| 50 | `rate_limit.py`, `upstream_safety.py`, `secrets/kms.py` (AES-256-GCM envelope encryption), `Caddyfile` (TLS + reverse proxy) |

**🟡 PARTIAL — the missing piece is named; build only that:**

| # | What exists | What is missing |
|---|---|---|
| 8 | StreamableHTTP gateway at `/mcp`; outbound client also StreamableHTTP-only | stdio and SSE gateway transports |
| 12 | Prometheus metrics wired at `/metrics`; OpenTelemetry tracing real but off by default (`settings.otel_enabled`) | structured/JSON log output — `main.py` still uses plain-text `logging.basicConfig` |
| 15 | paste, upload, direct URL, GitHub (repo/tree/blob/raw + discovery + OAuth repo picker), SwaggerHub — all five end to end. `upload` is now a distinct source that records the filename as provenance | nothing outstanding |
| 16 | `openapi_spec_validator` against the official 3.0/3.1 schemas, first error surfaced | a Spectral-style rule layer (rule IDs, severities, best-practice checks) and multi-error reporting |
| 17 | 3.0.x and 3.1.x normalized | Swagger 2.0 → 3.x conversion (currently rejected outright with an actionable message) |
| 24 | `apiKey` in header, `http` bearer/basic; untranslatable schemes emit warnings | query/cookie `apiKey`; a real OAuth2 grant flow (oauth2/openIdConnect degrade to a pasted bearer token); more than one active scheme |
| 26 | JSON request bodies (`application/json`, `+json`) | form-urlencoded, multipart, and binary bodies — non-JSON bodies currently emit `unsupported_body` and are dropped |
| 28 | Sutr's hosted gateway proxies compiled tools dynamically; packaging emits standalone servers | the two are separate code paths with no shared mode toggle |
| 29 | Generated servers offer `--transport stdio\|http` | plain SSE transport in generated servers |
| 36 | deploy / start / stop / delete / status | `sync`/`update` (redeploy with a new package) — today it is delete-and-recreate |
| 37 | status polling via `provider.status()`, `/logs` endpoint (Cloud Logging for Cloud Run, CloudWatch for App Runner), `/health` in generated servers | metrics collection (CPU/memory/request counts); the generated `/health` probe is never scraped by Sutr; **Azure Container Apps logs** live in Log Analytics, a different API with a different token audience, so `logs()` returns a portal link rather than output |
| 40 | Compiled tools called through Sutr's gateway route through the full approval pipeline | a generated package running on your own infra executes with no approval hook — **by design**, and it must be stated as a limitation, not silently fixed |
| 42 | Browse grid with text search over bundled + custom | a marketplace proper: categories, tags, install counts, dedicated route |
| 45 | Both SDKs exist and are CI-tested (`sdk/python`, `sdk/typescript`) | publishing — `@sutr/sdk` and `sutr-sdk` both 404 on their registries |
| 48 | Durable `usage_event` ledger (never pruned) + real Stripe customer/checkout/portal/webhook | quota or plan-limit enforcement — zero `quota` references in the server; only IP/org rate limits exist |
| 51 | 4 workflows: backend lint, frontend lint, tests (server + both SDKs), fly deploy gated on pytest | CLI build/test job, npm/PyPI publish automation, security scanning (CodeQL / dependency audit) |

**❌ NOT BUILT:**

| # | Note |
|---|---|
| 35 | Swaraj Cloud adapter — the name appears **only in comments**. Do not invent its API; obtain real docs from the user first |
| 39 | Deployed-MCP identity/authz — `docker_provider.py` binds the endpoint to 127.0.0.1 and has no auth of its own, which is why the provider is disabled on multi-tenant instances (`deploy/registry.py`) |

**Four deployment providers exist:** `docker` (local), `gcp` (Cloud Run), `azure` (Container
Apps), and `aws` (App Runner). There is still **no Kubernetes provider** and no GitOps/Argo
app lifecycle; `deploy/registry.py` mentions those only as future work.

**The three cloud providers are implemented against each cloud's documented REST APIs and
covered by tests at the HTTP layer, but have not been run end to end against live paid
accounts.** Treat their request bodies as unverified-in-production. The local Docker provider
has been exercised live.

Known limitations already documented (respect, don't "discover" them): TOTP secrets are stored
in plaintext; the SSRF guard resolves DNS at validation time and does not pin the address against
rebinding; rate-limit and approval-event state is per-worker (in-process), so multi-worker
deployments enforce per-worker budgets; datadog dual-token storage is singular; `EnvVarAuth` is
dead code; org deletion button inert; SQLite `PRAGMA foreign_keys=ON` deferred pending
delete-ordering review.

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

**Additional reference — the spec-import flow.** [`sanketwork300-hash/apitomcp`](https://github.com/sanketwork300-hash/apitomcp)
is the author's own prototype of the fetch → normalize → compile → generate pipeline. Take from
it: the **source-adapter boundary** (`backend/sources/github_source.py`,
`backend/sources/swaggerhub_source.py`, `backend/swaggerhub/client.py`) — provider knowledge
stays in an adapter and never leaks into the pipeline; the SwaggerHub URL host set and
`/apis|/apis-docs/{owner}/{api}/{version}` path forms; the raw-API-key (non-Bearer) auth header;
and the spec-filename priority list. Sutr's equivalent is `server/src/sutr/openapi/sources.py`.
Do **not** copy its structure wholesale — it has no tenancy, no governance, and no SSRF guard.

## 4a. The canonical spec → MCP flow (do not restructure this)

The builder is a seven-stage wizard in `ui/src/pages/McpBuilderPage.tsx` (stage components in
`ui/src/components/mcp-builder/`), backed end to end by real endpoints. Each stage maps to one
server capability:

| Stage | UI step | Server |
|---|---|---|
| 1 | **Source** — GitHub (connected account with a repo picker, or a pasted token), SwaggerHub, Upload, Direct URL, or Paste | `POST /api/openapi/import`, `GET /api/connections/github/repos` |
| 2 | **Choose file** — shown only when a repository holds more than one spec | `POST /api/openapi/discover` |
| 3 | **Normalize & IR** — validated, `$ref`s resolved, operations/servers/security extracted | `openapi/normalizer.py` + `resolver.py` |
| 4 | **Select tools** — per-operation checkboxes, tag filter, search | `filters.include_operations` |
| 5 | **Authentication** — header + `{token}` format, prefilled from the spec's security schemes | `openapi/security.py` |
| 6 | **Build** — dry-run preview, then compile; optionally download the package | `POST /api/openapi/{id}/compile`, `/package` |
| 7 | **Deploy** — local Docker, Cloud Run, Container Apps, or App Runner; the form is rendered from the provider's declared `config_fields` | `GET /api/deployments/providers`, `GET /api/connections/{id}/targets`, `POST /api/deployments` |

Rules that must hold:

- **Credentials are pinned to their provider's host.** A `github_token` is only ever attached to
  a request whose host already resolved to GitHub; likewise SwaggerHub. There is a regression test
  for exactly this (`tests/test_openapi/test_sources.py::test_github_token_travels_only_to_github`).
  Source tokens are used for one request and **never persisted**. A *connected account* is
  different and is persisted, through the secrets backend — see `connections/store.py`.
- **A connection is a personal grant.** It is scoped to (org, user, provider); one member
  must never be able to deploy under another's cloud identity, and an API key cannot open
  one at all (it has no identity to revoke).
- **`redirect_after` on a connection callback must be a relative path.** An open redirect
  there turns an OAuth flow into a phishing hop; anything else is discarded, not sanitized.
- **AWS has no OAuth for its own APIs.** The OAuth path for AWS is IAM Identity Center's
  `sso-oidc` device grant; only that token is stored, and every operation exchanges it for
  short-lived role credentials. Do not add a stored access-key path without saying so.
- **Discovery ranks, it does not decide.** `_rank_candidate` orders `openapi.*` before `swagger.*`,
  shallower before deeper, YAML before JSON, merely spec-shaped names last. The first entry is a
  default selection the user can override with `path`.
- **A generic `url` import auto-detects GitHub/SwaggerHub links** and routes them through the
  adapter, because a raw GET on a repository URL returns an HTML page and fails with a parse error
  that explains nothing.
- **Compiled tools reuse `CustomApiIntegration`** so they travel the same install → discovery →
  approval → execution path as every other integration. Never give generated tools their own
  execution path.
- **Every token and URL field carries `help` text** naming the scope needed, why it is needed, and
  that it is not stored. This is a product requirement, not decoration.

## 5. Build order

**Phases 4–14 of the original plan are complete** (shared execution pipeline, governance,
OpenAPI compiler, HTTP runtime, generated servers, local Docker deployment, observability and
metering, SDKs, frontend, security hardening, E2E validation). See `SUTR_PROGRESS.md` for what
each phase delivered. The remaining work is the 🟡/❌ list in §3, ordered by dependency:

**Next — request-body coverage (row 26).** Form-urlencoded, multipart, and binary bodies in
`openapi/normalizer.py` (stop dropping them at `_pick_json_content`) and `api_client.py`
(serialize per media type). Highest-value gap: today any non-JSON-body operation compiles to a
tool that cannot send its payload. Must also flow into `openapi/packaging.py`'s `RUNTIME_PY` so
generated servers behave identically.

**Then — security-scheme coverage (row 24).** Query and cookie `apiKey`; multiple active schemes.
A real OAuth2 client-credentials/authorization-code grant for compiled APIs is a separate,
larger piece — do not fold it in silently.

**Then — Kubernetes deployment provider (rows 33, 34, 36, 37, 39).** One new class behind the
existing `DeploymentProvider` ABC. Requires: a `sync`/`update` path (today it is
delete-and-recreate), metrics scraping of the generated `/health` probe, and an identity/authz
story for the deployed endpoint — which is the actual blocker, not the manifests.
**Swaraj Cloud (row 35) comes after, and only once the user supplies real API docs.**

**Then — quota enforcement (row 48).** The `usage_event` ledger already records everything
needed; what is missing is plan limits and a refusal path. Refusals must not be metered, matching
the existing rule for gated and rate-limited calls.

**Then — spec linting (row 16)** and **structured JSON logs (row 12)**, both self-contained.

**Then — publishing (rows 45, 51).** Publish `@sutr/sdk` and `sutr-sdk`; add the CLI to CI and
automate releases; add dependency/code scanning.

**Deliberately not planned:** Swagger 2.0 conversion (row 17 — rejecting with an actionable
message is the decision on record) and approval hooks inside generated standalone packages
(row 40 — they run on the operator's own infrastructure by design).

Each unit ends with the verification gate + a `SUTR_PROGRESS.md` entry. Do not start one early
because it "seems easy".

## 6. Definition of done (per phase)

1. Verification gate green (server tests ≥ previous count; UI + CLI builds clean).
2. New behavior covered by pytest/vitest tests, including failure paths.
3. `docs/pages/` updated in the same commit for any API/model change.
4. `SUTR_PROGRESS.md` entry: built / decisions / limitations / test count.
5. No renamed `/api/*` routes, no edited applied migrations, no reformatted unrelated files.
