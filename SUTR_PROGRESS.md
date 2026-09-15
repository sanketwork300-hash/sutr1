# Sutr Build Progress

Working log of the Sutr → Sutr build. See `SUTR_PHASE0_AUDIT.md` for the baseline audit.
Verification gate for every phase: `cd server && uv run ruff format && uv run ruff check . && uv run pytest -q` plus `cd ui && pnpm exec tsc -b && pnpm exec vite build` and `cd cli && pnpm exec tsc --noEmit`.

## Phase 0 — Repository audit ✅ (2026-08-19)
- Full audit in `SUTR_PHASE0_AUDIT.md`. Baseline: 328 server tests green.

## Phase 1 — Foundation ✅ (2026-08-19)
- Request-ID + security-headers ASGI middleware (`request_context.py`, `main.py`); always-on production exception handlers (were dev-only) returning `request_id` on 500s.
- Startup schema guard `check_database_schema()` — fails loudly when DB is behind migration head (warns in dev).
- `LOG_LEVEL` setting; `pool_pre_ping` for Postgres; alembic `env.py` missing `Secret`/`Subscription` imports fixed (autogenerate would have emitted DROPs).
- Proxy-trust plumbing: `TRUST_PROXY_HEADERS`/`FORWARDED_ALLOW_IPS` in `start.sh`, enabled in `docker-compose.prod.yml` + `fly.toml` — fixes per-IP rate limits collapsing behind Caddy/Fly.
- Registration password floor (6 chars, matching change/reset); fly deploy gated on tests in CI; dev-deps unified; `.env.example` expanded.
- Deferred (documented): SQLite `PRAGMA foreign_keys=ON` (needs delete-ordering review — `delete_secret` call sites run while `oauth_state` rows still reference the secret), `.env` `override=True` flip.
- Tests: `tests/test_foundation.py`. Suite: 336 green. Verified live: boot, `/health`, `/api/config`, `/mcp` 401 + RFC 9728 header, headers present.

## Phase 2 — Identity & organizations ✅ (2026-08-19)
Server:
- Migration `0023`: `workspace` (default per org backfilled), `org_invitation`, `org_membership.created_at`, `user.token_version`.
- `authz.py`: roles `owner|admin|developer|member|viewer`, permission matrix, `require_permission()` dependency, `ensure_agent_can()` (API keys keep documented capabilities; only human contexts are role-checked). RBAC decision documented: role matrix, not ReBAC — flat resource graph.
- Org lifecycle (`api/orgs.py`): org info/rename, member list/role-change/remove (last-owner protection, owner-only ownership changes), invitations (hashed single-use 7-day tokens, email + one-time `invite_url`), public preview/accept (`/api/org-invitations/*`); accepting never creates an org (works on self-hosted); new accounts start verified + get an access token.
- Workspaces (`api/workspaces.py`): CRUD + non-deletable default; `get_or_create_default_workspace()` for later phases.
- Sessions: `POST /api/auth/logout` (revocation list); `user.token_version` ("tv" claim) invalidates all JWTs on password change/reset (REST + MCP + refresh tokens); reset also clears lockout.
- TOTP is now a real login factor (`totp_code` form field on `/api/auth/token`; recovery codes accepted/burned).
- Deterministic multi-org resolution: `default_membership()` (oldest membership wins) replaces `.first()` everywhere incl. MCP legacy path; `oauth_provider` no longer hard-fails on ≠1 memberships; `X-Org-ID` header selects an org (verified against membership).
- Enforcement wired: org-settings PATCH (was completely unchecked!), tool-settings PUT, approval decisions ×3, api-keys create/revoke.
- `maintenance.py` hourly loop: prunes expired revoked tokens + stale google-login state, materializes expired approvals (fixes status=pending filter lying).
- Invitation email template (HTML-escaped).
UI:
- Login TOTP step; `/join` invitation page; Settings → Organization panel (rename, members, roles, invite w/ copyable link, revoke); logout now revokes server-side; API client namespaces `org`, `orgInvitations`, `workspaces`.
- Fixed 4 latent type errors revealed once `tsc` actually ran (deprecated `baseUrl` made tsc 6 exit before typechecking — removed): nonexistent `api.approvals.approveForever` (removed the broken "approve forever" button — backend feature never existed), `env_var` narrowing in ConnectDialog, two `detail.error` accesses.
- Tests: `test_orgs_workspaces.py`, `test_identity_phase2.py`. Suite: 358 green. Live E2E (fresh DB, real HTTP): 18/18 — register→login→rename→invite→accept-signup→RBAC denials→promotion→last-owner protection→logout revocation.
- Docs: api.md — Organization/Workspaces sections, login `totp_code`, logout, tool-settings `deny` mode documented.
- Known limitations: no org deletion (Danger Zone button still inert), approvals `approve-exact-forever` still unbuilt, `tools:execute` viewer restriction not yet enforced on the execution paths (Phase 5), UI has pre-existing eslint errors (not in CI).

## Phase 3 — Native integrations ✅ (2026-08-19)
- Contract tests over all 49 bundled integrations (`tests/test_integrations/test_bundled_contract.py`): unique well-formed ids, https URLs, coherent auth declarations (`{token}` in formats, RFC 7230 headers, https OAuth endpoints for provider-configured), valid ApiTool/CustomTool definitions (methods, path-param declarations, JSON types, unique names), categories reference real tools. Found + fixed: 5 orphaned `tool_categories` entries in resend for tools that don't exist.
- **SSRF hole closed on the custom-MCP path** (the audit's biggest security gap): `validate_safe_url` now enforced at custom-MCP create/update (`api/custom_mcp.py`), at install (`api/installed.py`), at connection time in `mcp/client.py` (`_ensure_safe_upstream`, `custom_` prefix only — bundled URLs are vendor constants), and before OAuth discovery in `auth_start.py`. Tests: `tests/test_api/test_custom_mcp.py` (router previously had ZERO tests).
- `integrations:manage` permission enforced on installed create/update/delete and custom-MCP/custom-API mutations via `ensure_agent_can` (API keys unchanged — no role, documented capabilities preserved).
- OAuth refresh helpers unit-tested for the first time (`tests/test_mcp/test_oauth_refresh.py`): expiry buffer, ExceptionGroup/cycle-safe auth-error detection, happy/failure/missing-material refresh paths.
- Known limitation (documented, unchanged): providers declaring two token headers (datadog DD_API_KEY + DD_APPLICATION_KEY) can only store one token (`InstalledIntegration.token_secret_id` is singular); remote-MCP token auth sends `Authorization: Bearer` regardless, so this is latent. `EnvVarAuth` remains dead code.
- Suite: 573 green.

## Phase 3.5 — OpenAPI compiler core (partial, discovered 2026-08-19)
Found in-tree but not logged: `sutr/openapi/` (loader, normalizer, resolver, security, compiler, limits, errors), `api/openapi_projects.py`, `models/openapi_project.py`, migration `0024`, 26 tests. Fixed on intake:
- Resolver cycle detection now tracks every JSON-Pointer location on the traversal path (definition sites included), so direct `A→A` and indirect `A→B→C→A` cycles become bounded placeholders at first re-entry (was: one extra expansion level, and self-refs at the definition site escaped detection). Push/pop set keeps deep documents linear.
- Compiler test fixtures were invalid OpenAPI (undeclared path params, duplicate operationIds) and correctly rejected by openapi-spec-validator; rewritten as valid specs whose operationIds collide only after snake_case normalization.
- `Warning_` → `SpecWarning` (ruff N801).
Remaining for the full OpenAPI phase: REST wiring/UI wizard/testing+deploy of compiled tools (§21–27 tail).

## Phase R — Platform-wide Sutr rebrand ✅ (2026-08-19)
Codemod across 229 files + renames, per the naming-consistency directive. All checks green after: ruff clean, **599 server tests pass**, `tsc`+`vite build` (UI), `tsc`+`tsup` (CLI), live boot verified (`/health`, `/api/config`, `/mcp` 401 + RFC 9728 `resource_name: "Sutr MCP"`).
- Python package `agent_port` → `sutr` (imports, pyproject `name = "sutr"`, uv.lock regenerated, alembic, Dockerfiles, start.sh, scripts).
- MCP meta-tools `agentport__*` → `sutr__*`; MCP server name + TOTP issuer + OAuth AS resource name → "Sutr"; `SutrOAuthProvider`.
- CLI: bin `sutr` (primary) + `ap` (compat alias), package `sutr-cli`, Commander program name `sutr`, `tools run` added as alias of `tools call`. Config migrates from `~/.config/agent-port/config.json`; `SUTR_URL` env (falls back to `AGENT_PORT_URL`).
- UI: title/labels/logos/favicons rebranded (placeholder "S" marks generated); localStorage token key migrates `agent_port_token` → `sutr_token` on first load (sessions survive); README hero diagram wordmark patched in-image.
- Deploy: compose services/images/volumes `sutr*`; default SQLite path `/data/sutr.db` with a guarded one-time `mv` from `/data/agent_port.db` in start.sh; fly.toml/install.sh/docs/Caddyfile renamed. README gains an "Upgrading from AgentPort" section + MIT attribution to upstream.
- **Decisions documented:** DB tables were never brand-prefixed (`user`, `org`, …) so no table renames were needed (the spec's `agent_port_users → sutr_users` example doesn't apply); REST stays under `/api/*` — it carries no branding, and renaming 100+ stable routes consumed by UI/CLI/docs would break compatibility for zero branding gain (the `/api` namespace *is* the Sutr API namespace). GitHub org/domain placeholders used where the real ones don't exist yet: `github.com/sutr-dev/sutr`, `app.sutr.sh`, `docs.sutr.sh` — update when the real remote/domains are provisioned. LICENSE intentionally untouched (upstream MIT attribution).

## Phase 4 — Canonical tool pipeline ✅ (2026-08-19)
REST and MCP no longer carry separate copies of the execution pipeline. New `sutr/services/` layer:
- `services/tool_pipeline.py` — the single implementation of policy evaluation, approval gating, upstream dispatch (OAuth pre-flight + one auth-error retry), logging, and analytics. Surfaces build a `CallContext` and translate `GateResult`/`ExecutionOutcome` into their own response shapes; `api/tools.py` and `mcp/server.py` are now thin translators (MCP's `_dispatch_and_log` deleted).
- `services/tool_catalog.py` — TTL-cached tool discovery moved out of `api/tools.py`. The MCP management tools keep their serve-stale-refresh-in-background read strategy (deliberate latency tradeoff, documented, not drift).
**Drift bugs fixed by unification** (each previously present on exactly one surface):
- REST wrote gate logs with `outcome="approval_required"`, which the UI and `/api/logs` expiry decoration only recognize as `pending` → REST-gated calls rendered wrong. Canonical outcome is now `pending` on every surface; readers stay tolerant of legacy rows (`GATE_LOG_OUTCOMES`), api.md updated.
- REST never recorded `requester_ip`/`user_agent` on logs or approval requests, never passed api-key metadata to `get_or_create_approval_request`, and never set `access_reason="approved_once"` on consumed approvals.
- MCP never recorded `duration_ms` and emitted no analytics; both surfaces now emit `tool_called`/`tool_call_denied_by_policy` with a `source` property (`api`/`mcp`).
- `result_json` is stored only on success (REST's expression already behaved this way by accident; now explicit).
- PostHog client is disabled when no project token is configured (self-hosted default) — the background consumer used to 401 on every flush.
Tests: `tests/test_services/test_tool_pipeline.py` (9 — gate semantics, in-place gate-log resolution, duration/metadata, REST regression for the outcome drift). MCP test fixtures now also patch `sutr.db.engine` (the pipeline resolves the engine lazily via `sutr.db`). Suite: **608 green**; live boot smoke-checked.
Remaining (deferred to Phase 5 governance): per-call upstream connection reuse (perf), decision-row locking, log redaction/retention.

## Phase 5 — Governance ✅ (2026-08-19)
Migration `0025`: `audit_event` table, `org.log_retention_days`, four composite `log_entry` filter indexes.
- **Audit trail** (`services/audit.py`, `models/audit_event.py`, `GET /api/audit` behind new `audit:read` permission — owner/admin). Rows are added in the same transaction as the action. Wired: login, logout-adjacent security events (password change/reset, TOTP enable/disable), API-key create/revoke, per-tool policy changes (**with old→new mode** — was destroyed invisibly), approval decisions ×4, integration install/uninstall (REST **and** MCP, with API-key vs user actor attribution), org rename/role-change/member-remove/invitations, org settings changes, admin impersonation start (recorded under the *target's* org).
- **Decision-row locking**: approval decisions transition `pending→decided` via a conditional UPDATE (`WHERE status='pending' AND expires_at>now`) — concurrent approve-vs-deny resolves to one winner, loser gets 409. `try_consume_approved_request` likewise consumes via conditional UPDATE — a grant executes at most once under racing agents. Decision endpoints deduplicated into a shared `_decide` scaffold.
- **Approve exact args forever**: new `POST /requests/{id}/approve-exact` (TOTP + RBAC like the others), `decision_mode="approve_exact_forever"`. The pipeline consults the standing grant after the approve-once consume; matching calls execute with `access_reason="approved_exact"` (the previously dead enum value), never consumed, exempt from request expiry (incl. in MCP `await_approval`). Changed args re-gate. UI: new button on the approve page.
- **`tools:execute` enforced on execution paths** (Phase 2's known gap): REST `call_tool` and MCP `execute_upstream_tool`/`await_approval` reject viewer-role users; API keys keep documented capabilities.
- **Log redaction** (`services/redaction.py`): LogEntry `args_json`/`result_json` are stored with credential-shaped keys (password/token/api_key/secret/…) and JWT/bearer-shaped values replaced by `[REDACTED]`. Approval-request args are deliberately NOT redacted — they are re-executed verbatim by await-approval.
- **Log retention**: `org.log_retention_days` (PATCH /api/org-settings, bounded 1–3650, absent-vs-null semantics), pruned in the hourly maintenance sweep. Audit events are exempt by design.
- **Arg canonicalization** (`approvals/normalize.py`): NFC unicode normalization + integral-float collapse (1.0 ≡ 1); booleans preserved. Note: pending approvals created pre-upgrade may need re-requesting (hash rule change).
- PostHog denied/executed events unchanged; audit is the durable record.
- Tests: `tests/test_services/test_governance.py` (11 — audit writes incl. old/new policy metadata, permission on /api/audit, double-consume, concurrent-decision 409, full approve-exact E2E via REST, viewer 403, redaction incl. approval-args exemption, retention incl. audit exemption, unicode/number hash equality). Suite: **619 green**; UI tsc+build green; migration applied on dev DB.
- Deferred (unchanged): multi-worker approval event bus, per-call upstream connection reuse, wildcard/category policies.

## Phase 6 — OpenAPI end-to-end ✅ (2026-08-19)
Completed the OpenAPI subsystem from compiler core (Phase 3.5) to a working product surface. No new execution machinery: compilation materializes a `CustomApiIntegration`, so generated tools flow through the canonical install/discovery/approval/execution pipeline.
- **REST layer verified + tested** (`tests/test_api/test_openapi_projects.py`, 8): import paste/URL (fetcher SSRF-screened: no private targets, no redirects, size cap), official-schema validation with typed 400s (Swagger 2.0 rejected), detail view (operations/tags/servers/suggested auth), dry-run compile previews without side effects, real compile creates the integration + catalog visibility, **recompile updates the same integration in place**, delete keeps the generated integration, viewer RBAC 403, and a pipeline E2E: compiled `list_pets` resolved from the generated tools_json and executed through `/api/tools/{id}/call` with the mock at the HTTP dispatch boundary, log row verified.
- **UI import wizard** (`OpenApiImportPage`, route `/integrations/openapi/new`, third card in the New-integration chooser): paste-or-URL import → summary stats + import warnings → configure (tag exclusion chips, deprecated toggle, spec-server picker incl. server-variable inputs with enum dropdowns, custom base URL, auth header/format prefilled from the spec's security schemes, integration name) → dry-run preview of the exact tools (renames + param counts + warnings) → create → hands off to the existing custom-API builder page for credentials/policies. `api.openapi` client namespace + full types.
- **CLI**: `sutr openapi import (--file|--url) | list | show | compile [--dry-run --include-tags --exclude-tags --exclude-paths --include-deprecated --server-url --name --auth-header --auth-format] | delete`.
- **Docs**: api.md "OpenAPI projects" section.
- **Live E2E smoke** (fresh DB, real HTTP): register → login → import → dry-run (`list_things, create_thing`) → compile → catalog shows `type=custom, auth=token`. Also confirmed the compile-time SSRF guard live: an unresolvable server host is refused with "Host could not be resolved".
- Suite: **627 green**; UI tsc+vite+eslint green; CLI tsc+build green.
- Deferred to later phases: standalone generated MCP server packaging (§28–31), OpenAPI project audit events, upload-file UI affordance (paste covers it), CustomApiBuilderPage regeneration hints.

## Phase 7 — Generated standalone MCP servers ✅ (2026-08-19)
Sutr spec §28–31: any compiled OpenAPI project can be exported as a self-contained MCP server package with no Sutr dependency.
- **Generator** (`openapi/packaging.py`): zip with `server.py` (MCP stdio server + `--list-tools`), `sutr_runtime.py` (request building/execution mirroring `api_client` semantics exactly — path quoting, query/header wire names, `body_param` wrapping, auth header merged over param headers), `tools.json` (compiled ApiTools + auth config — pure data), **generated offline tests** (`test_server.py`: bundle shape, schemas, request building for every tool, path-param URL-encoding, args-cannot-override-auth-header), `Dockerfile`, `pyproject.toml`/`requirements.txt` (mcp + httpx), README with Claude Desktop/Code config + Docker instructions, `.env.example`. Injection-safe by construction: templates are static text, all API-specific content lives in `tools.json`. Byte-deterministic output (fixed zip timestamps). Credentials only via a slug-derived env var.
- **Endpoint** `POST /api/openapi/{id}/package` (same body as /compile), `integrations:manage`, audited (`openapi.package_generated`). Deliberate decision, documented: the package base URL is NOT SSRF-screened — packages run on the user's own infrastructure where private-network APIs are legitimate.
- **CLI** `sutr openapi package <id> [--out] [filter/server/auth flags]`; **UI** "Server package" download button on the wizard's preview step (`requestBlob` helper in the client).
- Tests (`test_openapi/test_packaging.py`, 11): content/determinism/no-auth variant/empty-refusal/slug sanitization; generated runtime imported and verified directly; **subprocess proofs** — generated `server.py --list-tools` runs, generated pytest suite passes; endpoint zip + audit + viewer 403.
- **Live E2E smoke** (Sutr on :4750, real upstream echo API on :4777): CLI api-key auth → `sutr openapi import --file` → `sutr openapi package` (14 KB zip) → extract → generated tests pass → `--list-tools` → **generated runtime executed real HTTP calls against the live upstream** (auth header delivered, query param passed, POST body echoed). User's real CLI config backed up/restored around the test.
- Suite: **638 green**; UI + CLI builds green.
- Deferred: streamable-HTTP transport option for generated servers, docker build verification in CI (needs a runner with Docker), deployment providers → Phase 8.

## Phase 8 — Deployment engine + local Docker provider ✅ (2026-08-19)
Sutr spec §32–38 with the local Docker provider first (user's call — no cluster targets yet). Migration `0026` adds the `deployment` table.
- **Generated servers grew an HTTP transport** (Phase 7 deferral resolved): `server.py --transport http --port N` serves MCP streamable HTTP at `/mcp` (stateless) + a JSON `/health` probe, via the mcp SDK's own Starlette/uvicorn deps — no new requirements. Boot-tested in the suite.
- **Provider abstraction** (`deploy/base.py`): `DeploymentProvider` (available/deploy/status/start/stop/remove/logs) over an opaque per-deployment state dict — K8s/Argo/Swaraj implement the same interface later. Registry gates providers: Docker is refused on `is_cloud` instances (tenant containers on the API host are not a multi-tenant-safe operation) and via `DEPLOY_DOCKER_ENABLED=false`.
- **DockerProvider** (`deploy/docker_provider.py`): docker CLI via async subprocess (no SDK dep) — build from the extracted package zip, run with loopback-only ephemeral port binding (`-p 127.0.0.1:0:8000` — the MCP endpoint has no auth of its own), `--restart unless-stopped`, memory/cpu caps, `sutr.deployment=<id>` labels; status/logs/stop/start/rm/rmi; idempotent removal. Security posture documented in the module.
- **Model/API/service**: `Deployment` row snapshots the package zip at create time (reproducible even if the project changes); runtime token via the secrets backend, injected as env at deploy time — never in the image or package; background build task (`services/deployments.py`) drives queued→building→running/failed with system audit events; reads reconcile stored status with live provider state. Endpoints: create/list/detail/logs/stop/start/delete + `/providers`; new `deployments:manage` permission; full audit (created/started/stopped/deleted/failed/cleanup_failed).
- **CLI** `sutr deploy providers|list|create|status|logs|stop|start|delete`; **UI** Deployments page (sidebar entry): list with live status pills + auto-poll while building, create dialog (project picker/name/token), logs viewer, start/stop/delete.
- Tests (17 new): DockerProvider CLI invocations + parsing with a faked binary; API lifecycle with a fake provider incl. background deploy, secret→env flow, status reconciliation, RBAC, audit; generated-server HTTP `/health` subprocess boot. Suite: **648 green**.
- **Live Docker E2E** (real daemon 29.7.2, real upstream echo API): CLI create → image built + container running in ~15s → `/health` OK → **full MCP session against the deployed container** (initialize, tools/list, tools/call — the call flowed container→host upstream with the secret-backed token delivered) → logs/stop/start/delete → zero leftover containers.
- **Bug caught by the live test, impossible to catch mocked**: pip inside the container resolved `mcp 2.0.0`, whose server API breaks the generated code (`Server.list_tools` gone). Generated packages now pin `mcp>=1.9.0,<2.0` (requirements + pyproject templates).
- Deferred: Kubernetes/Argo CD/Swaraj providers (need real targets — same interface), deployment health-probe polling in maintenance loop, UI deploy-from-wizard shortcut.

## Phase 9 — Observability & metering ✅ (2026-08-19)
Closes the audit's §16/§46/§60 gaps: billing had no durable usage source and the platform had zero instrumentation. Migration `0027` adds `usage_event`. New deps: `prometheus-client` (base), `opentelemetry-sdk`/`-exporter-otlp-proto-http` (optional `otel` extra, plus a dev dep so spans are really tested).
- **Usage ledger** (`models/usage_event.py`, `services/metering.py`): tool calls are metered **in the same transaction as their log entry** from the canonical pipeline, so both surfaces meter identically and an execution can neither be logged without being metered nor metered without running. Gated/denied calls are deliberately *not* metered (no upstream work) but stay logged+audited. `deployment_runtime` accrues in 5-minute samples from the monitor sweep (sampling under-bills on a crash rather than over-billing). Retention never prunes the table; events carry counts and dimensions only, never args/results.
- **`/api/usage/summary` + `/events`**: SQL-side aggregation (totals by kind, outcome/source/integration/tool breakdowns, daily series, avg/max latency), 366-day cap, org-scoped.
- **Prometheus** (`observability/metrics.py`, `/metrics`): tool calls/duration/gated, approval decisions, HTTP requests/latency, deployment gauge. **Disabled by default** (404 — existence unadvertised); `METRICS_TOKEN` adds bearer auth. **Documented decision: no org/integration/tool labels** (cardinality + tenant disclosure to whoever scrapes); per-tenant numbers live in the ledger. HTTP series use the matched *route template*, never raw paths — an ASGI middleware reads the route Starlette merges into the scope.
- **OpenTelemetry** (`observability/tracing.py`): off by default, optional dep, no-op helpers so call sites need no conditionals. `sutr.tool_call` spans carry integration/tool/source/access-reason/outcome/duration and record exceptions — never args, results, or credentials (traces leave the process). Verified with a real in-memory SDK exporter, plus explicit "no secret in attributes" assertion.
- **Deployment monitoring** (Phase 8 deferral): `deployment_monitor_loop` reconciles status with providers every 5 min, meters running time, and sets the gauge; survives provider errors per-deployment.
- **UI** Usage page (sidebar): stat tiles for headline numbers, a one-series daily bar chart with per-bar hover (no legend — the title names the series), ranked tables that double as the table view. Chart hue is a token pair validated with the dataviz palette validator: `#4f46e5` passes on the light surface; `#818cf8` **failed** the dark lightness band (0.68 > 0.67) so dark mode uses its own selected step `#6366f1` — selected, not flipped. **CLI** `sutr usage summary|events`.
- Tests (27 new): pipeline metering incl. the not-metered-when-gated rule, retention exemption, aggregation correctness, org-scoping, API-key access, tz-aware inputs; metrics endpoint gating/token/labels/route-templates/counters; real OTel spans; deployment sweep metering/reconciliation/gauge/error tolerance. Suite: **675 green**.
- **Live E2E** (real HTTP to api.github.com/zen through a no-auth custom API): `/metrics` 401→200 with token, two real executions, ledger shows `tool_calls=2 executed:2 api:2 avg 430ms`, Prometheus counters + route-template HTTP series moved, **no tenant id anywhere in /metrics**, CLI summary renders — and tracing confirmed active in the boot log.
- **Two real bugs the live run caught that the mocked tests could not**: (1) `/api/usage` used a human-only JWT dependency, so the CLI (and future SDKs, which authenticate with API keys) got 401 — switched to `get_agent_auth` + `ensure_agent_can("logs:read")`, matching the other agent-facing endpoints; (2) both the UI and CLI send JS `toISOString()` values ending in `Z`, and comparing offset-aware input against this codebase's naive-UTC columns raised `TypeError` → 500. Both are now pinned by regression tests.
- Deferred: usage-based billing quotas (the ledger is the source; pricing rules are a product decision), per-workspace usage attribution, OTel spans on the approval long-poll.

## Phase 10 — SDKs ✅ (2026-08-20)
Spec §44: official Python and TypeScript SDKs over the now-stable REST surface. Both live under `sdk/`, are independently installable/testable, and are wired into CI (`test.yml` gains `sdk-python` and `sdk-typescript` jobs).
- **Python** (`sdk/python`, dist `sutr-sdk`, import `sutr_sdk`, only dep httpx): `Sutr` + `AsyncSutr` with an identical surface, enforced by a parity test. Every decision (URL building, error mapping, retry eligibility) lives in `_core.py`; the two clients differ only in transport — the Phase 4 anti-drift lesson applied. Import name deliberately ≠ the server's `sutr` package so both can share an interpreter. Typed `Tool`/`ToolResult`/`ApprovalDecision`/`Integration` (each keeping `.raw`), full error hierarchy carrying the server's `request_id`. Tests: 42 (httpx `MockTransport`, no new deps).
- **TypeScript** (`sdk/typescript`, `@sutr/sdk`, zero runtime deps, native fetch): same surface, same error names, `AbortController` timeouts, emitted `.d.ts`. Tests: 36 (vitest, scripted fetch).
- **Approval flow is the centrepiece**: a gated call raises `ApprovalRequired` (with `approval_url` + id), a blocked one `ToolDenied`; `wait_for_approval` / `waitForApproval` long-polls the decision and then **re-issues the byte-identical request** (an approval is bound to the exact args a human saw — asserted in both suites). Timeout surfaces `ApprovalPending`, not a generic failure.
- **Documented decisions**: tool calls are *never* retried (side effects; a duplicate refund beats a visible error) while idempotent reads retry on 429/5xx honouring `Retry-After`; N818 is deliberately suppressed for `errors.py` because `ApprovalRequired`/`ApprovalPending`/`ToolDenied` are expected governance outcomes, not faults, and the two SDKs share the names.
- **`/api/logs` left human-only on purpose**: an API key can call tools but must not read *other* callers' arguments and results, so `logs()` requires a user token and both SDKs document/raise accordingly — the opposite call from Phase 9's `/api/usage` (aggregates, safely key-readable). Both boundaries are asserted live.
- Docs: new `connect/sdk` page; sidebar now also exposes `api` and `tool-approvals`, closing a Phase 0 audit gap.
- **Live cross-language E2E** (one server, real HTTP to api.github.com, a background thread/timer standing in for the human approver): both SDKs did list → call → gated-call-raises → **approved mid-flight and completed** (199 KB payload) → usage → integrations → API-key-denied-logs → user-token-logs → async client. Ledger accounting verified exactly: `get_zen:3, get_meta:2`, with the four gated calls correctly **absent** (Phase 9's not-metered-when-gated rule holding across SDKs).
- **Two real bugs the live run caught that unit tests missed**: (1) an ambient `SUTR_API_KEY` silently overrode an explicitly passed `access_token`, sending the wrong credential — env is now a fallback, never an override, in both SDKs, with regression tests; (2) `await_approval` hot-looped if a server/proxy answered "pending" instantly — added a shared 500 ms poll floor (`poll_backoff`) in both.
- Suites: Python SDK **42**, TS SDK **36**, server **675** (unchanged), UI/CLI typecheck clean.
- Deferred: publishing to PyPI/npm (needs registry credentials), streaming tool results, an MCP-transport client (the gateway already speaks MCP for that case).

## Phase 11 — Frontend completion ✅ (2026-08-20)
Closed the audit's remaining UI gaps (§48–49). Everything else on that list landed in earlier phases (org/members Phase 2, OpenAPI wizard Phase 6, deployments Phase 8, usage Phase 9).
- **Approvals inbox** (`/approvals`, sidebar entry): the action queue the platform lacked — filter by pending/approved/denied/all, per-request tool + integration + summary + the agent's `additional_info`, argument-key preview, requester IP and API-key label, live expiry countdown, and a Review link into the existing decision page. Expired requests are separated and dimmed rather than hidden; pending view self-refreshes every 15s because expiry is on a clock.
- **Activity page** (`/activity`) with two tabs: global **tool-call logs** (reusing the existing LogCard/LogDetailPanel, filterable by outcome) and the **audit trail** (Phase 5's API), rendered as a dense timestamp/action/summary/actor table with expandable JSON metadata. A 403 renders as "owners and admins only" rather than a raw error.
- **Workspaces panel** in Settings: list/create/rename/delete with the default workspace marked and undeletable, and 403s explained in words.
- Two real client gaps found while wiring: `api.logs.list` didn't accept the `outcome` filter the server has supported all along, and there was no `approvals.list`/`audit.list` at all.

## Phase 12 — Security hardening ✅ (2026-08-20)
Migration `0028`. Took the audit's tractable high-value items; the rest are recorded as known limitations rather than silently skipped.
- **Tool-call rate limiting** (the audit's "nothing on tools/MCP" gap): per-org sliding window enforced *inside the canonical pipeline*, so REST and MCP are limited by one implementation — checked before policy evaluation so a runaway loop costs one in-memory check, not a DB round trip and an approval row. REST returns `429` + `Retry-After`; MCP returns a message telling the agent to stop looping. Refusals are deliberately **not logged and not metered** (no upstream work happened) and are counted in `sutr_tool_calls_gated_total{reason="rate_limited"}`. `TOOL_RATE_LIMIT_PER_MINUTE` (default 120, 0 disables).
- **Secret storage hygiene**: dropped `secret.value_hash` (unsalted SHA-256 of the secret — offline-checkable) and `secret.prefix` (its first 12 plaintext characters — often enough to identify an API key). Both were **write-only**: nothing in the codebase ever read them, so they were pure leakage, including under the KMS backend. A comment on the model warns against reintroducing any value-derived column.
- **Stripe webhook idempotency**: `processed_stripe_event` claims each event id in the same transaction as the handler's writes, so Stripe's retries can't re-apply effects while a *failed* handler still rolls the claim back and stays retryable. Concurrent deliveries resolve via the primary-key conflict.
- Known limitations, unchanged and documented: TOTP secrets remain plaintext at rest; SSRF DNS-rebind pinning is still open (the dispatch-time re-check is in place — see review-comments.md); the in-memory limiter counts per worker.

## Phase 13 — Review findings resolved ✅ (2026-08-20)
Re-verified all four findings in `review-comments.md` against the current code and locked each with a regression test (`tests/test_security/test_review_findings.py`, 10 tests). Three were already fixed; **P3 was still open and is now fixed**: the custom-API PATCH handler distinguishes an absent field from an explicit `null` via `model_fields_set`, so a cleared description actually clears. Tests also cover the *inverse* of each fix (the legitimate stored-token case still authenticates; an invalid merged auth pair is still rejected; an omitted description still doesn't wipe the stored one), so no fix degraded into permissiveness. `review-comments.md` is rewritten as a resolution record, including the one part that remains open (DNS pinning) rather than claiming a clean sweep.

## Phase 14 — Full-platform validation ✅ (2026-08-20)
One scripted run over a live server and a real upstream API asserting each phase's invariant: **22/22 checks passed**.
Identity → org rename → workspaces → invitation · OpenAPI import → dry-run compile (tag filter excludes admin, nothing created) → compile → standalone package whose **own generated tests pass** · install → allow/deny policy → real upstream dispatch → 403 on denied · audit trail carrying login/org/integration/policy/key events with old→new modes · ledger metering exactly the executed calls · logs recording both outcomes · Python SDK raising `ToolDenied` · deployment providers · CLI over the same instance · rate limit returning 429 and, finally, the accounting proof: with a 10-call budget, 3 tokens went to gated calls and 7 to executions, and the ledger showed exactly 8 executed — **denials and 429s consume budget but are never billed**.
Two external/self-inflicted issues surfaced and were handled honestly rather than papered over: PowerShell 5.1 mis-tokenizes UTF-8 box characters without a BOM (script rewritten ASCII-only), and the validation's own 120-call rate-limit test exhausted GitHub's 60/hour unauthenticated budget — so the upstream assertion now checks *reachability and Sutr-side recording* rather than a 200, and the test runs last with a low limit.

## Phase 15 - MCP builder sources, and a re-audit of all 51 rows (2026-08-20)
Driven by three flaws the user found in the shipped build, plus a request for a build prompt that cannot mislead a future agent.

- **Spec sources (GitHub + SwaggerHub).** New `openapi/sources.py` keeps all provider knowledge in adapters so the pipeline stays source-agnostic. GitHub: URL parsing for repo/tree/blob/raw forms, default-branch resolution, one recursive git-trees walk (capped at 20k entries / 50 candidates), contents-API fetch with base64 decode, size cap, and `download_url` fallback; HTTP statuses map to actionable codes (`github_auth`, `github_rate_limited`, `github_not_found`). SwaggerHub: `app.`/`portal.`/`api.swaggerhub.com` hosts, `/apis|/apis-docs/{owner}/{api}/{version}` paths, the API key sent **raw** (SwaggerHub does not want a Bearer prefix), `?resolved=true` to inline refs. Reference for the shape of this: the user's own `apitomcp` prototype.
- **Discovery ranks, it does not decide.** `_rank_candidate` orders `openapi.*` before `swagger.*`, then shallower before deeper, then YAML before JSON, with merely spec-shaped names last. `POST /api/openapi/discover` returns the ordered list and the UI shows a chooser when a repo holds several specs; the first entry is only a default. A `/tree/` URL scopes the walk to that subdirectory, which needed a distinct `GitHubTarget.directory` field - reusing `path` had made a tree URL look like a file.
- **Credentials pinned to their provider's host.** A `github_token` is only ever attached to a request whose host already resolved to GitHub, likewise SwaggerHub; `test_github_token_travels_only_to_github` asserts it. Source tokens serve one request and are never persisted, which is also what the UI's help text promises.
- **A generic `url` import now auto-detects GitHub/SwaggerHub links** and routes them through the adapter. Before this, pasting a repository URL into the plain URL source fetched the HTML repository page and failed on a parse error that explained nothing. The stored `source_kind` records what actually happened, not what was asked for.
- **UI: `McpBuilderPage`** replaces `OpenApiImportPage` and `CustomApiSetupPage` (both deleted) with the six stages the user asked for - Source, Choose file, Normalize & IR, Select tools, Authentication, Build & deploy - with a clickable stepper, per-operation checkboxes plus tag/text filters, an expandable IR view, auth presets with `{token}`-placeholder validation, and a local-file picker on the Paste source. Operations without an `operationId` are marked and explained: the compiler's filters key on `operationId`, so they can only be excluded by tag.
- **Every token and URL field carries `help` text** naming the scope required, why it is needed, and that nothing is stored - the user's third flaw. Includes the SwaggerHub "key as-is, not a Bearer token" caveat and the direct-URL note that private hosts are refused and redirects are not followed.
- **Custom API builder retired** (the user's second flaw): the chooser now offers MCP server or Build-from-an-API only, and `/integrations/custom-api/new` redirects to the builder rather than 404ing. The OpenAPI path produces the same `CustomApiIntegration` rows but derives paths, parameters, and auth from a specification instead of asking the user to retype them.
- **CLI kept level with the UI**: new `sutr openapi discover <url>`, and `import` gained `--path`, `--github-token`, `--swaggerhub-key` (with `SUTR_GITHUB_TOKEN` / `SUTR_SWAGGERHUB_API_KEY` as fallbacks). The CLI does not re-implement source detection - the server owns that rule.
- **Live end-to-end proof** against real GitHub and the public Swagger Petstore: discovery resolved the default branch (`master`) and found `src/main/resources/openapi.yaml`; import produced 19 operations, the real server URL, and provenance; a one-operation selection compiled to exactly `get_inventory`; dry-run created nothing; install, then the first call **was gated by the approval policy**, approve-once released it, and the retry dispatched for real. A bogus operationId was refused with `nothing_selected`. The petstore itself answered 500 on the final hop, which Sutr surfaced as `isError` with the upstream status rather than masking - correct behaviour, and worth stating plainly.
- **One test broke and it was worth keeping**: copying `ui/dist` into `server/ui_dist` for single-port local runs made the SPA catch-all shadow any route registered after startup, so the unhandled-exception test got 200 instead of 500. The test now inserts its route at the front and therefore holds in both layouts, and `server/ui_dist/` is gitignored as the build artifact it is.
- **Re-audited all 51 functionality rows against the code** (four parallel readers, every claim carrying a file:line). The old status section in `SUTR_BUILD_PROMPT.md` was written before Phases 4-14 and had become the single biggest hallucination risk in the repo; it is replaced with 33 implemented rows (each with its evidence), 16 partial rows (each naming only the missing piece), and 3 not-built rows. Two corrections to earlier assumptions: `sutr-cli` **is** published on npm at 1.1.1, and both SDKs are **not** published (registry 404s). The build prompt also gains the canonical six-stage flow and the rules that must hold within it.
- Tests: 36 new (`test_openapi/test_sources.py` 35, plus the auto-detect regression in `test_api/test_openapi_projects.py`). Suite: **732 green**, ruff clean, UI `tsc`/`vite` and CLI `tsc` clean, and eslint clean on every file touched.
- Newly documented gaps, not silently skipped: non-JSON request bodies are still dropped at `_pick_json_content` (so a multipart or form operation compiles to a tool that cannot send its payload) - now the top of the remaining-work list; query/cookie `apiKey` and real OAuth2 grants are unsupported; there is no Kubernetes provider and Swaraj Cloud exists only in comments.

## Phase 16 - Connected accounts (OAuth) and cloud deployment targets (2026-08-21)
Driven by the user's request: remove the custom-API path from "new integration" (already done in
Phase 15), give the OpenAPI import an OAuth route into GitHub alongside SwaggerHub and a real
upload, say plainly in the UI what every URL and token field is for, put all of it in the MCP
builder, and add AWS/Azure/GCP as deployment targets - with OAuth for authorization.

- **Connected accounts** (`connections/`, `api/connections.py`, models `provider_connection` +
  `oauth_connect_state`, migration `0029`). One mechanism for two uses that are the same grant
  underneath: GitHub authorizes *reading a specification*, the clouds authorize *running a
  server*. `providers.py` holds every provider fact, `flow.py` runs authorization-code + PKCE,
  `device.py` runs the AWS device grant, `store.py` owns persistence and the single
  `access_token()` entry point that refreshes when due. Tokens are `secret` rows through the
  configured backend; the connection row holds pointers, an expiry, and a display label.
- **AWS has no OAuth for its own service APIs**, so the AWS path is the one place AWS really does
  speak OAuth: IAM Identity Center's `sso-oidc` device authorization grant (RFC 8628). Only the
  Identity Center token is stored; every operation exchanges it for short-lived role credentials
  via `GetRoleCredentials`. `authorization_pending` and `slow_down` are treated as normal states
  of a flow in progress, not failures - otherwise every flow aborts the moment a user takes more
  than five seconds to read the code.
- **The callback is the sharp edge and is treated as one.** It arrives as a bare browser redirect
  with no session, so the `state` row is the only thing binding a code to a user: single-use,
  15-minute TTL, deleted before the exchange whatever the outcome, and `redirect_after` is
  refused unless it is a relative path (an open redirect there is how an OAuth flow becomes a
  phishing hop). Seven API tests cover replay, expiry, denial, provider mismatch, cross-user
  polling, and both open-redirect shapes.
- **A connection is a personal grant**, scoped to (org, user, provider). Another member cannot
  borrow it to deploy under someone else's cloud identity, cannot revoke it, and an API key
  cannot open one at all - it belongs to the org, not to a person, so there would be no identity
  to revoke. Disconnecting deletes sutr's tokens and says, in the UI, that it does *not* revoke
  the app on the provider's side.
- **Three cloud deployment providers**, each building the image with that cloud's own build
  service and running it on its serverless container runtime: `gcp_provider.py` (GCS -> Cloud
  Build -> Artifact Registry -> Cloud Run), `azure_provider.py` (ACR source upload -> ACR Task ->
  Container Apps), `aws/provider.py` (S3 -> CodeBuild -> ECR -> App Runner). SigV4 is implemented
  in `deploy/aws/sigv4.py` rather than taking boto3 as a hard dependency for a handful of calls.
- **The provider interface grew a `ProviderTarget`** (credentials + placement) on every method,
  because cloud credentials are short-lived by design and caching them on the row would be wrong.
  `deploy/credentials.py` resolves them fresh per operation. Providers also declare their own
  `config_fields`, so the builder renders a provider's form without knowing the provider - adding
  a provider is a server change only.
- **Decisions worth naming.** Cloud Run has no stopped state (it already scales to zero), so
  *stop* switches ingress to internal-only, which makes it genuinely unreachable without
  destroying the revision; Container Apps and App Runner have real stop/pause and use them.
  Delete removes only what belongs to that deployment - the resource group, registry,
  environment, bucket, and ECR repository are shared and are left alone. Azure's registry admin
  user is enabled rather than using a managed identity, because a managed identity needs a role
  assignment on the subscription that many users' accounts cannot make, and failing on an IAM
  technicality is worse than a documented registry credential in the app's own secret store.
  App Runner's `StartCommand` semantics against an existing ENTRYPOINT are ambiguous, so the
  buildspec re-tags the image with an explicit `CMD` instead - unambiguous, and it makes the
  published image runnable as-is.
- **Two IAM roles are asked for, not guessed.** CodeBuild needs a service role and App Runner
  needs an ECR access role; inventing names for them and failing cryptically would be worse than
  asking. Both are validated before anything is created, and the exact policies are documented.
- **MCP builder: seven stages.** `upload` became a real source (drag-and-drop, 8 MB cap, filename
  kept as provenance) instead of an alias of `paste`. GitHub gained a connected-account option
  with a repository picker - private repos included - while the pasted-token path stays, because
  it is the only one that works for an API-key caller, for a repo the user would rather not
  connect wholesale, and for an install whose operator registered no OAuth app. A new **Deploy**
  stage renders each provider's form from its declared fields, with target pickers fed by the
  connected account's own inventory: a project id or account number typed from memory is the
  single most likely thing to be wrong, and the wrongness only shows up minutes into a build.
- **Every field says what it is for.** Which scope a token needs, why, whether it is stored, that
  SwaggerHub wants its key unprefixed, that a direct URL must be reachable from the *server* and
  that redirects are not followed, that an uploaded file is read in the browser and never stored,
  and - for each deploy provider - exactly what a deploy will create in the user's account.
- **Extracted `ui/src/components/mcp-builder/primitives.tsx` and `styles.ts`** from the 1820-line
  builder page so the new stage components share one Card/Field/SecretInput vocabulary; a wizard
  assembled from several files still has to read as one wizard.
- **SigV4 was verified, not assumed.** The implementation matches botocore's `SigV4Auth` /
  `S3SigV4Auth` byte-for-byte across five cases at a frozen clock, including two of AWS's own
  published test vectors (`get-vanilla`, `get-vanilla-query-order-key-case`). botocore is not a
  test dependency; the expected signatures are pinned in `tests/test_deploy/test_sigv4.py`.
- Tests: **113 new** (connections 39, cloud providers + cloud deployment API 38, SigV4 8, sources
  and import 8, plus the existing deploy tests updated for the new interface). Suite: **845
  green**, ruff clean, UI `tsc`/`vite`/eslint clean on every file touched, CLI `tsc`/`tsup` clean,
  docs site builds.
- Live-verified against a running server: provider listing with per-provider "not configured"
  reasons naming the exact env vars and callback URL, a real GitHub authorization URL with PKCE,
  the 503/409 refusals, deploy-provider config fields, an upload import recording its filename,
  and a bogus callback state redirecting to `connection_status=error&connection_error=invalid_state`.
  Migration `0029` applies, rolls back, and re-applies cleanly.
- **Stated plainly rather than implied by a green test: the three cloud providers have not been
  run end to end against live paid accounts.** They are implemented against each cloud's
  documented REST APIs and covered at the HTTP layer; the local Docker provider is the one that
  has been exercised live. Also newly documented: Azure Container Apps logs live in Log Analytics
  (a different API with a different token audience than the deploy grant covers), so `logs()`
  returns a portal link rather than an empty string that would read as "the server printed
  nothing".
- Docs: new `pages/mcp-builder.md` (all seven stages, every field, every URL shape),
  `pages/connected-accounts.md` (what a connection is, both flows, storage, expiry),
  `pages/self-host/connected-accounts.md` (registering each OAuth app, callback URLs, scopes, the
  two AWS IAM roles), plus rewritten Connected-accounts and Deployments sections in `api.md`.
- CLI kept level: new `sutr connections list|connect|targets|disconnect` (the device grant is the
  one flow a terminal can finish end to end), `--use-connection` on `openapi discover|import`,
  `--file` now imports as `upload` with its filename, and `deploy create` gained `--connection`
  and a repeatable `--config key=value`.

## Phase 17 - Google's hosted MCP servers as bundled integrations (2026-08-21)
Ten first-party Google remote MCP endpoints added, in `integrations/bundled/google_mcp/`.

- **Verified before trusting.** The supplied list of endpoints looked synthesized, so every URL
  was probed live: a control host (`totallyfakeservice98765.googleapis.com`) returns Google's
  HTML 404, while the real ones answer a proper MCP `initialize`. Ten of the listed URLs are
  real MCP servers; the Google Cloud ones (BigQuery, Cloud Storage, Cloud Run, Compute Engine)
  have **no published fixed endpoint** and were not added. Tool names, counts, and descriptions
  come from each server's own `tools/list`, not from the table.
- **Auth is ordinary Google OAuth.** These sit behind Google's standard API frontend: no
  `WWW-Authenticate`, no `/.well-known/oauth-protected-resource`, and an unauthenticated
  `tools/call` answers "Expected OAuth 2 access token". So they are `remote_mcp` integrations
  with `provider="google"` and per-product scopes - installing Sheets must not hand out mailbox
  access. Only the two Cloud APIs (Maps Code Assist, Developer Knowledge) take `cloud-platform`,
  and a test enforces that.
- **Gmail and Calendar are added alongside the existing REST integrations, not over them.**
  Replacing `gmail`/`google_calendar` would change tool names under anyone who had already
  installed them, silently invalidating their per-tool approval policies. The two are also
  genuinely different: Google's Gmail server publishes **no send tool** (drafts only), while
  Sutr's REST one sends; Google's Calendar server adds `suggest_time` and semantic
  `search_events`, which the REST one has no equivalent for. Both facts are in the descriptions
  and pinned by tests, so if Google adds sending the test failing is the prompt to stop telling
  users something untrue.
- Live end-to-end: the catalog serves all 12 Google entries, `POST /api/installed` on
  `google_drive` succeeds, and the OAuth start produces a correct Google authorization URL with
  the Drive scope and the registered `OAUTH_CALLBACK_URL`.
- Tests: 66 new (`test_integrations/test_google_mcp.py`); the catalog-size tripwire moved
  49 -> 59 deliberately. Suite: **953 green**, ruff clean.
- Docs: `google-oauth-setup.md` now covers all twelve, with per-integration API-enablement and
  scope tables, and an explicit warning that `OAUTH_GOOGLE_*` (integrations) is not
  `GOOGLE_LOGIN_*` (sign-in) - though one Google client can serve both if both redirect URIs
  are registered on it.

## Phase 18 - ESDS evolution, Phase 0 (audit) and Phase 1 (completing partial features) (2026-08-27)

Target architecture: *ESDS Sovereign Agent Platform - Developer LLD (Condensed v1.1)*. The work is
a **backward-compatible evolution** of Sutr, not a rewrite: the 953-test baseline stayed green
through every step and every `/api/...` route kept its shape.

### Phase 0 - audit (no code changed)

Four documents, all grounded in the code as it actually is rather than as it is described:

- `docs/ESDS_IMPLEMENTATION_GAP.md` - what Sutr covers, what the LLD asks for, and the delta,
  section by section, with the three places Sutr deliberately contradicts the LLD called out
  (one database vs database-per-service; monolith vs 15 services; `/api` vs `/v1`).
- `docs/ARCHITECTURE_DECISIONS.md` - 17 ADRs, each Context/Decision/Consequences/Reversibility.
- `docs/REFERENCE_MAP.md` - the reference project consulted per feature, and for each one an
  explicit **dependency verdict**: Keycloak, OpenFGA, Casbin, OPA, Composio, Spectral,
  openapi-generator, openapi-diff and Temporal are behavioural references only and become
  dependencies of nothing.
- `docs/ESDS_TRACEABILITY_MATRIX.md` - every LLD requirement, once, with a status from the
  mandated vocabulary. The LLD's headline numbers (99.95%, 100k invocations/min, 10k agents) are
  recorded as **NOT TESTED** design targets, and ADR-017 forbids changing that without a
  recorded k6 result.

### Phase 1 - completing Sutr's partial features

**Structured logging (Feature 12, ADR-012).** `main.py` no longer uses `basicConfig`'s text
format. `observability/log_format.py` emits one JSON object per line carrying the LLD §5.3 field
set - correlation, tenant, agent, provider, tool, runtime, status, duration, region - populated
from a context variable (`log_context.py`) that middleware and the execution pipeline bind, so a
log line five frames down cannot omit them. Redaction happens **in the formatter**, reusing
`services/redaction.py`'s rules, because a rule every call site must remember is a rule that will
be broken; a test logs a bearer token and asserts it does not appear in the output.
`X-Correlation-ID` is now honoured inbound and echoed outbound alongside `X-Request-ID`.

**OpenAPI linting (Feature 16, ADR-013).** `openapi/linting/` - 31 rules across four modules,
each finding carrying `rule_id`, `severity`, `message`, `location`, an RFC 6901 JSON pointer,
a documentation anchor and remediation text. Rule ids match Spectral's `oas` ruleset where the
same check exists; Sutr-specific ones carry a `sutr-` prefix. Spectral itself is **not** a
dependency - it is a Node toolchain and the stack is Python. Structural validation now returns
**every** violation (`iter_errors`) instead of the first. New: `GET /api/openapi/lint/rules`,
`POST /api/openapi/lint`, findings on import errors and on project detail. The rule reference at
`docs/pages/openapi-linting.md` is **generated** from the registry
(`server/scripts/generate_lint_docs.py`) so a finding's documentation anchor cannot dangle.

**Swagger 2.0 conversion (Feature 17, ADR-014).** `openapi/swagger2.py` converts 2.0 to 3.0
before anything else sees the document, so the pipeline has one dialect. Host/basePath/schemes to
servers, definitions to components with `$ref` rewriting, body parameters to `requestBody` using
`consumes`, `formData` to form or multipart bodies, `produces` to per-response content,
`securityDefinitions` with OAuth2 flow renaming, `type: file` to binary. What cannot be mapped
produces a named warning. The IR records `source_dialect` so the original dialect survives.

**Request bodies of every encoding (Feature 26, ADR-008).** Previously anything that was not
`application/json` emitted a warning and produced a tool that posted nothing. Now the IR carries
an encoding (`json | form | multipart | binary | text`) and the declared media type verbatim;
the compiler maps each to parameters (multipart binary parts become base64 arguments, because
MCP arguments are JSON and there is no other honest way to carry bytes); both runtimes construct
the right request.

**One runtime definition (Feature 28, ADR-010).** `runtime/request_builder.py` is now the single
definition of how a compiled tool becomes an HTTP request - dependency-free, stdlib only. The
hosted gateway imports it; `openapi/packaging.py` **embeds its source verbatim** into every
generated package. The generated runtime is not a copy that resembles the gateway's, it *is* the
gateway's. A parametrised test runs both implementations over json/form/multipart/binary/text and
asserts the two request descriptions are equal.

**Complete security translation (Feature 24, ADR-009).** `openapi/security.py` returns a list of
credential *placements* rather than one header. API keys in **header, query, and cookie**; bearer;
basic; OAuth2 with the grant kept intact rather than flattened. Client-credentials is **executed
by the platform** - token endpoint called, token cached, refreshed before expiry, token URL
SSRF-screened - which is what build prompt §24 means by not degrading OAuth2 into a pasted token.
Authorization-code routes to connected accounts. mTLS and implicit/password grants are reported as
unsupported *with the reason*, never silently dropped. New `integration_credential` table
(migration 0030) holds one credential per scheme, secret references only, plus
`GET/PUT/DELETE /api/integrations/{id}/credentials`. Credentials are applied after arguments and
overwrite them, so an argument can never displace a credential.

**Quotas (Feature 48, ADR-011).** New `quota` table (migration 0031) and `services/quota.py`.
Limits are daily/monthly calls and concurrency, scoped to the tenant, an integration, or a single
tool. Evaluation runs in `evaluate_gate` **before policy and before any provider contact**; a
breach returns **429** with `Retry-After` on REST and an explanation an agent can act on over MCP.
A refused call is logged and metered at **quantity 0** with outcome `quota_exceeded`, so the
refusal is auditable without being billed as an execution - a test asserts exactly that. Counts
derive from the usage ledger rather than a second counter, because a second counter is a second
truth that can disagree with the bill. `GET/PUT/DELETE /api/quotas`.

**MCP transports (Feature 8).** SSE gateway at `GET /sse` + `POST /messages`, and a stdio entry
point (`sutr-mcp-stdio`, identity from `SUTR_API_KEY`). All three transports serve the *same*
`mcp_server` object, so policy, approvals, logging and metering apply identically - a test pins
that. A POST to `/messages` is checked against the org that opened the session, so knowing a
session id is not enough to inject into someone else's stream.

**Generated servers (Features 29, 40, ADR-007).** Generated packages gain `--transport sse`
alongside stdio and streamable HTTP, and a `sutr_governance.py` module implementing
`GOVERNANCE_MODE`:
- `standalone` (default) states in the README, the `.env.example` and the startup log that the
  deployment is **independent of Sutr's approval policies**;
- `platform` validates a signed access pass on every call - signature, issuer, audience, expiry,
  single-use nonce, tenant, and the tools the pass covers - and **refuses to start** when the
  validation material is missing, because a governance mode that verifies nothing would report as
  enforced while enforcing nothing. It also refuses stdio, which carries no per-call pass.

**Deployment update, rollback, history, metrics (Features 36/37, ADR-016).** `DeploymentProvider`
gains `update()`, `rollback()`, `metrics()` and `supports_update`/`supports_metrics` flags. A
`DeploySpec` now carries a revision number and an `artifact_tag` derived from it, so every revision
builds a **distinct, retained artifact** - which is what makes a rollback a re-run of something that
actually ran rather than a rebuild from source that has since changed. New `deployment_revision`
table (migration 0032) keeps the package, config, provider state and outcome of every version;
existing deployments are backfilled with revision 1 so their history starts at what is running.
`POST /{id}/update`, `POST /{id}/rollback`, `GET /{id}/revisions`, `GET /{id}/metrics`. The Docker
provider preserves its published port across an update, so the deployment's URL survives, and
reports CPU/memory from `docker stats`. The three clouds report readiness and replica configuration
from the resources they already fetch, and **name the monitoring API they would need** for CPU,
latency and request counts rather than inventing numbers - the same honesty already applied to
Azure logs.

**Swaraj Cloud (ADR-006).** `SwarajCloudProvider` implements the full interface and none of the
behaviour. Every operation refuses with `NOT_CONFIGURED / DOCUMENTATION_REQUIRED`, naming the nine
things that must be documented before it can be written. It is **registered and visible** rather
than hidden, so a user asking "can I deploy to Swaraj Cloud?" gets an answer. Tests pin the refusal
so it cannot quietly become a guess.

**Marketplace (Feature 42).** A storefront on its own routes, separate from `/api/integrations`
which the CLI and console already depend on. `integrations/categories.py` is a curated taxonomy -
12 categories covering **all 59** bundled integrations with none left in "Other", plus tags that
combine a curated list with each integration's own tool groupings. Install counts come from the
install table, ratings from a new `marketplace_review` table (migration 0033) where one person
holds one opinion per integration, so a rating cannot be inflated by resubmission.
`GET /api/marketplace/listings|categories|tags`, `GET/PUT/DELETE .../reviews`. A new
`/app/marketplace` console route with search, category and tag filters, sorting, paging, and a
detail drawer. **The Registry-owned fields - trust score, compliance, regions, pricing, versions -
are returned as explicit nulls with a stated reason, never as zeros**: a trust score of 0 would
read as "untrustworthy", where null reads as "unknown".

**CI and publishing (Features 45/51).** Five new workflows. `security.yml` runs the LLD §5.7 gate
order - dependency audit (pip-audit, pnpm audit), Gitleaks over full history, Semgrep, Trivy on the
image and the tree, Syft SBOM - failing on CRITICAL findings with a fix available.
`codeql.yml` covers Python and TypeScript. `cli.yml` adds the missing CLI build and typecheck.
`release.yml` is the release gate in order: tests → build → **scan before push** → SBOM → push →
Cosign keyless sign → SBOM attestation. `publish-sdks.yml` publishes `sutr-sdk` to PyPI via trusted
publishing and `@sutr/sdk` to npm with provenance, **tag-triggered only**, each verifying the tag
matches the manifest version before uploading anything. Per ADR-017 the SDKs are **not** marked
published: the workflow exists and has not run.

**CLI.** New `sutr marketplace search|show|categories|rate` and `sutr quota list|set|remove`. No
existing command changed.

### Verification

Suite grew **953 -> 1239** (all green, ~68s); ruff clean; UI `tsc` + `vite build` and CLI `tsc` +
`tsup` green; migrations 0030-0033 each apply, roll back, and re-apply. No existing route changed
shape. Four tests were deliberately rewritten because the behaviour they pinned is what the build
prompt asked to change: Swagger 2.0 is now converted rather than refused, a query API key is now
supported rather than dropped, `oauth2` with no declared flow is now refused rather than degraded to
a pasted token, and multipart bodies now compile rather than warning.

**Live-verified against a running server**, not only through the test client:

- logs come out as one JSON object per line carrying the full LLD §5.3 field set;
- the marketplace serves 59 listings across 12 categories with **zero** uncategorised, and a rating
  written through the API appears on the listing;
- a messy specification returns **15 findings at once** (6 error, 5 warning, 4 info) with rule ids,
  JSON pointers and remediation, instead of one error per round-trip;
- a Swagger 2.0 document with a `formData` file upload and a **query** API key imports, converts to
  OpenAPI 3.0.3, produces `https://example.com/v2` as the server, and compiles into a multipart tool
  with both fields — and the query credential is correctly reported as one the single-header storage
  model cannot hold;
- `GET /sse` and `POST /messages` return 401 unauthenticated (with `WWW-Authenticate`), and a POST
  to a session the caller does not own returns 404;
- the deployment provider list shows Swaraj Cloud **visible and disabled** with its
  `DOCUMENTATION_REQUIRED` reason;
- with a daily quota of 1: call 1 → **200**, calls 2-4 → **429 with `Retry-After: 18509`** and a
  body naming the quota, the limit and the usage. The usage ledger then reads
  `executed: 1, quota_exceeded: 3` and the quota reports `used: 1` — the three refusals are visible
  and audited but **not billed**, which is the property build prompt §48 asks for.

### Known limitations, stated rather than implied

- Concurrency quotas and the approval event bus are per process; with several workers the
  effective concurrency limit is `limit x workers`. Redis is the fix and is not done.
- Token and data-transfer quotas can be stored but are **not enforced** - nothing records those
  numbers per call yet, and the evaluator logs that rather than silently passing.
- Generated-server pass replay protection is per process, so a pass could be replayed once per
  replica within its lifetime. Passes are short-lived for that reason.
- Nothing issues access passes yet; `GOVERNANCE_MODE=platform` is ready for Provisioning, which
  is Phase 8 work.
- `MAX_OPERATIONS=300` still caps imports well below the LLD's 10,000+; the cap is deliberate DoS
  protection for untrusted input and needs to become tier-scoped, not removed.
- Cloud deployment `metrics()` reports readiness and replica *configuration* only. CPU, memory,
  request counts and latencies need Cloud Monitoring / Azure Monitor / CloudWatch — different APIs
  with different token audiences than the deploy grants cover. Each provider names the API it would
  need rather than returning zeros.
- The three cloud providers are still **NOT TESTED** against live paid accounts, unchanged from
  before. Update and rollback are covered at the HTTP layer and live-exercised only on Docker.
- The SDK publishing workflows exist but have **never run**; neither SDK is published. The CI
  security workflows likewise have not run in this environment — they are authored, and their YAML
  is validated, which is not the same as green.
- `pnpm prettier --check` fails on **8 pre-existing UI files** untouched by this work
  (`McpBuilderPage.tsx`, `SettingsPage.tsx`, and six components). The frontend-lint workflow is
  therefore red independently of these changes; fixing them was left out as unrelated churn.
- Marketplace reviews are org-local and are not published anywhere; the listing's aggregate rating
  counts every org on the instance, while the review *list* endpoint is org-scoped.
- Pre-existing and unrelated: `alembic downgrade base` fails inside migration **0016** (`DROP INDEX
  user_email_lower_uq`), which dates from the baseline commit. Migrations 0030-0033 each apply,
  roll back and re-apply cleanly on a fresh database — verified individually and as a group.

## Phase 19 - ESDS Phase 2: the foundation (2026-08-28)

Service boundaries, shared contracts, the event model, correlation IDs, idempotency, database
ownership, standard errors and API versioning. All of it additive: `/api/...` did not change shape,
and every route behaves identically to a client that does not opt in.

### Service boundaries and the plane split (ADR-001, ADR-002, ADR-022)

`platform/boundaries.py` declares 21 services across three planes, the modules each owns, and the
tables each **writes**. `tests/test_platform/` turns the declaration into three enforced rules:

1. Every module belongs to exactly one service — an unowned module fails the build. It caught the
   `/v1` package the moment it was added, which is the point.
2. Every table is written by exactly one service. This is database-per-service (LLD §2.4) in the
   form a single database can hold: other services read through the owner, never by joining.
3. **Data-plane modules do not import control-plane modules** — the testable form of "existing
   tools keep working when the control plane is down" (LLD §2.2).

Rule 3 found **24 violations** on its first run. The tempting fix was to keep reclassifying
offenders as "shared" until it passed. That would have made the suite green and the architecture
diagram false. Instead:

- Genuine misclassifications were fixed. Authentication, policy evaluation and the metering write
  really are hot-path primitives — the LLD's own data-plane lifecycle (§3.2) begins with
  Authentication and Tenant Resolution — so they moved to `shared`. `mcp.management_tools` really
  is a control-plane surface that happens to speak MCP.
- One was fixed properly: `credentials/store.py` split into `read.py` (hot path) and `store.py`
  (control plane), so the executor no longer imports the writer.
- The remaining **4 are recorded as debt** in `KNOWN_PLANE_VIOLATIONS`, each naming the phase that
  removes it (Phase 7's Runtime Router, Phase 9's PDP/PEP). The test fails on a *new* violation and
  also on a *stale* entry, so the list can only shrink; another test caps its length so adding to
  it is a decision, not a habit.

The §2.2 guarantee is therefore **not claimed anywhere**. It is measured, and the measurement is 4.

### The event backbone (ADR-003, ADR-024)

`events/` — envelope, topic catalog, transactional outbox, bus, relay, idempotent consumers.

- **39 event types** from the LLD's catalog (§3.1, §5.6), every one with a partition key derived by
  the LLD's own rules: `provider.*`→provider_id, `tool.*`→tool_id, `runtime.*`→runtime_id,
  invoices→invoice_id, policies→policy_id. Names that nothing emits yet are written down anyway —
  they become a contract the moment the first one is published.
- **The outbox is the producer contract.** `events.publish(session, ...)` adds a row to the
  caller's session and does not commit, so a fact and its announcement commit together or not at
  all. A test imports a bad specification and asserts the rejection announced nothing.
- **The relay is the only publisher**, and stops a batch at the first failure rather than skipping
  past it: publishing on would put a later fact about an entity ahead of an earlier one, which is
  what partition ordering exists to prevent. Retry → dead-letter after 5 attempts → manual retry
  through `POST /v1/events/{id}/retry`, which is the LLD's manual-investigation step.
- **Consumers are idempotent centrally.** Delivery is at-least-once, and "handlers must be
  idempotent" is a rule every handler must remember — so `(consumer, event_id)` is recorded before
  the handler runs, and a handler is simply not called twice. A failed handler releases the record
  so redelivery re-runs it, and one consumer's failure does not silence another's.
- **Kafka is a backend, not a dependency.** The default in-process bus needs no broker, so the
  single-container install keeps working. Selecting Kafka without the client reports an unavailable
  backend rather than failing at import.

Events are now emitted from real lifecycle points: `api.uploaded`, `translation.completed`,
`mcp.generated`, `tool.registered`, `runtime.deployed/updated/rolled_back/failed`. `usage.recorded`
is implemented but **off by default** — the ledger row is already durable and authoritative, and
nothing consumes the event yet, so emitting one would double the write volume of every invocation
for no reader.

### The shared library (LLD §2.6) and `/v1` (ADR-004, ADR-021)

`common/` — errors, responses, pagination, idempotency.

- **Standard errors** with the LLD's status contract and a stable machine-readable `code`. One
  exception handler renders them, so a service raises without knowing it is served over HTTP.
- **Cursor pagination**, opaque base64. Offset paging is wrong for a growing table: rows inserted
  between requests shift every later page. A forged cursor is a 400 naming the parameter, not a 500.
- **Idempotency** (ADR-023): opt-in per request, and the key is reserved **before** the work runs —
  reserving afterwards would let two concurrent retries both find no record and both do the work.
  Wired into `POST /api/deployments`, which build prompt §76 names.
- The envelope's exact shape is **ours, and recorded as such** (ADR-021): the condensed LLD renders
  it as a graphic whose field names do not survive text extraction, so guessing them and calling
  them the LLD's would be an invention presented as a specification. Tracked as open item B-8.

New surface: `GET /v1/platform/capabilities` (which backends are live and what is degraded, with a
reason each), `GET /v1/platform/services`, `GET /v1/events`, `GET /v1/events/dead-letter`,
`POST /v1/events/{id}/retry`.

### Contracts (build prompt §74, §75)

`contracts/events/` — 39 JSON Schemas, generated from the code and committed, so a change shows up
in a diff. `contracts/openapi/platform.yaml` — the `/v1` surface.
`scripts/check_contracts.py` verifies both against the code and runs in CI, which additionally
regenerates and fails on any diff. Avro/Apicurio is **not** done: it needs a running registry to
validate against, and the envelope carries `event_version` so moving later is a serialization
change, not a contract change.

### Correlation IDs

An inbound `X-Correlation-ID` is honoured, echoed, carried in the log context, embedded in every
event, and queryable: `GET /v1/events?correlation_id=...` returns one logical operation's whole
chain.

### Verification

Suite **1239 → 1353** green; ruff clean; migration 0034 applies, rolls back, and re-applies;
`check_contracts.py` passes.

**Live-verified against a running server:**

- one import + compile, tagged `X-Correlation-Id: phase2-trace-001`, produced the LLD's
  control-plane chain in order — `api.uploaded → translation.completed → mcp.generated →
  tool.registered` — every one `published` by the relay, each naming its producer service, all four
  returned by a single correlation-id query;
- cursor pagination walked two pages with no overlap and no cursor on the last;
- a forged cursor returned **400** in the standard error shape with `details.parameter = "cursor"`;
  `limit=0` and `limit=999` both **400**;
- `POST /api/deployments` twice with one `Idempotency-Key` returned the **same deployment id** both
  times and left exactly **one** deployment; the same key with a different body returned **409**;
- `/v1/platform/capabilities` reported 5 degraded capabilities, each naming its own reason.

### Known limitations, stated rather than implied

- **The data plane does not yet survive a control-plane outage.** Four imports break it, listed in
  `KNOWN_PLANE_VIOLATIONS`. Phases 7 and 9 remove them; until then the LLD §2.2 guarantee is not
  claimed.
- The Kafka backend is **NOT TESTED** against a broker. It is exercised only for its unavailable
  path. Avro serialization and Apicurio registration are not implemented.
- With more than one API replica, `EVENT_RELAY_ENABLED` must be false and one `sutr-event-relay`
  worker run instead, or replicas contend for the same outbox rows. There is no lease or advisory
  lock to enforce that yet.
- Consumer retry is redelivery-driven: a failed handler releases its record and is retried when the
  event is next dispatched. There is no scheduled redelivery of already-published events, so a
  handler that fails on the in-process bus is retried only if something re-dispatches.
- `/v1` covers platform and events only. Existing resources stay on `/api` under ADR-004; the
  standard envelope is not retrofitted to them.
- Table ownership is declared and enforced, but nothing prevents a cross-service *query* at
  runtime — the rule is checked on imports, not on SQL.

## Phase 20 - ESDS Phase 3: source connectors and translation (2026-08-28)

Source connectors with all seven verbs, continuous sync, drift detection and classification, IR
versioning and determinism, and a translation pipeline that reports every stage. Additive
throughout: `/api/openapi/import` behaves exactly as before, and no existing project starts polling
anything.

### Connectors (LLD §3.3, build prompt §12)

`source_connectors/` — one interface, seven verbs, **seven working connectors**:

| | watch | discover | |
|---|---|---|---|
| `paste`, `upload` | no | no | content handed to us; nothing to re-fetch, so nothing to watch |
| `url` | yes | no | polled with `If-None-Match` / `If-Modified-Since` — an unchanged document costs a 304 and no body |
| `swagger_ui` | yes | yes | **new**: finds the specification behind a Swagger UI page, which the LLD calls out because "many teams only know the Swagger UI URL" |
| `github` | yes | yes | polled by **commit SHA** — one small API call, no file transfer |
| `swaggerhub` | yes | no | polled by re-fetching; SwaggerHub has no conditional request, and the watch plan says so |
| `postman` | no | no | **new**: Collection v2.0/v2.1 → OpenAPI 3.0 |

Four verbs are connector-specific; **three are implemented once on the base class** (ADR-028). A
document's validity has nothing to do with where it was found, and drift is a property of the API
rather than the transport — requiring each connector to implement them would guarantee seven
slightly different answers to the same question.

Three source types the LLD names are **declared and not built**, each with its reason, and returned
by `GET /v1/sources/connectors` alongside the working ones: `wsdl` (the XML-Schema-to-JSON mapping
has enough judgement in it to be wrong quietly), `api_gateway` (**BLOCKED** — the LLD says "API
gateways" without naming which; open item B-3), and `generic_git`. A source type that is silently
missing looks like an oversight; one listed as unavailable with a reason is a decision.

The Postman converter is honest about what it is (ADR-029): a collection records *example requests*,
not a contract, so parameter types are inferred from example values and the conversion **returns
notes saying so**. An `Authorization` header becomes a security scheme, never a parameter — emitting
it as one would put a credential in the tool's argument list.

### Drift detection and classification (build prompt §13, ADR-025, ADR-026)

`openapi/diff.py` compares the **canonical IR**, never the documents (ADR-025). A reformatted YAML,
a reordered key map, or a migration from Swagger 2.0 to OpenAPI 3.x produces **no drift** — a report
that fires on whitespace trains people to dismiss drift reports, and then the one that matters gets
dismissed too. A test pins the dialect migration case: same fingerprint, zero changes.

Every difference is classified BREAKING / NON_BREAKING / SECURITY / DOCUMENTATION / METADATA,
judged from the **caller's** side. Two judgements worth stating:

- **Every security difference is SECURITY, whichever direction it moves.** Adding authentication is
  not "non-breaking because it is safer" — existing callers stop working. Removing it is not
  "non-breaking because callers keep working" — the API just became public.
- **A response disappearing from the document is DOCUMENTATION, not BREAKING.** The server may still
  return it.

### Continuous sync, and the rule that gates it

New `api_source` and `drift_report` tables (migration 0035), with the provenance field set build
prompt §13 fixes: source type, uri, version, commit sha, etag, last-modified, retrieved-at, plus
both hashes. A project has one primary source and any number of comparison sources.

`services/source_sync.py` fetches conditionally, distinguishes "the file moved" from "the API
moved", diffs, classifies, and then **consults the apply policy**:

| `apply_policy` | Behaviour |
|---|---|
| `never` (default) | every change waits for a human |
| `non_breaking` | applied unless something breaks — **or unless it is a security change** |
| `always` | applied; exists, is not the default, and choosing it is a decision |

A withheld change records **why**, because "nothing happened" must be explicable. Sync updates the
project's definition and **never compiles or deploys** (ADR-027): each of those has its own gate and
its own audit trail, and a background loop should not reach through them.

Failure handling: exponential backoff capped at 16×, and watching **paused** after 20 consecutive
failures with the reason on the row rather than the source just going quiet.

### IR versioning and determinism (build prompt §14)

`openapi/fingerprint.py` — `IR_VERSION`, a canonical form, and a content hash. Determinism that is
intended but unchecked stops being true, so it is asserted: the same specification produces the same
fingerprint, survives a JSON round trip, and is unaffected by reformatting or by advisory output
(warnings and lint findings are excluded — two runs that describe the same API are the same IR even
if one also mentioned a missing licence field).

### The translation pipeline, made inspectable (LLD §3.4)

`openapi/pipeline.py` reports six stages — parse → convert → validate → lint → resolve → normalize —
each with status, duration and what it produced. The LLD's stage *names* (AST, semantic graph) are
not adopted, because Sutr has no separate semantic-graph pass and inventing one to match a diagram
would add a layer that does nothing. What the LLD is asking for is the *property*: when translation
fails, you can see which stage and why. **Semantic errors halt before IR generation**, as §3.4
requires — a test asserts `normalize` never runs and `ir_hash` stays null.

### API

`GET /v1/sources/connectors`, `POST /v1/sources/discover`, `POST /v1/sources/validate`,
`GET/POST/PATCH/DELETE /v1/sources`, `POST /{id}/check`, `POST /{id}/compare/{other}`,
`GET /{id}/drift`, `POST /drift/{id}/apply`, `POST /drift/{id}/dismiss`. All enveloped and
cursor-paginated per Phase 2. New events: `source.connected`, `source.changed`,
`source.drift_detected`.

### Verification

Suite **1353 → 1478** green; ruff clean; migration 0035 applies, rolls back, and re-applies;
contracts regenerated (15 `/v1` paths) and passing.

**Live-verified against a running server:**

- the connector list served all seven working connectors plus the three unavailable ones with their
  reasons;
- attaching a source pointing at `http://127.0.0.1:4860` was **refused** — `Unsafe URL: Host
  resolves to a blocked network range`. The SSRF screen treating a loopback source as hostile is
  correct behaviour, and it is why the rest of the live run used a paste source;
- a baseline source was attached, its content replaced with a version that removed an operation,
  and `POST /check` returned **`breaking: 1, applied: false`** with the reason *"1 breaking
  change(s) were detected and the apply policy is 'non_breaking', so the change was not applied"*;
- the drift report listed `BREAKING: operation_removed`, `METADATA: api_version_changed`,
  `DOCUMENTATION: operation_documentation_changed` — and **the project still had both operations**;
- `POST /drift/{id}/apply` moved it to `applied`, and only then did the project drop `deletePet`;
- the event log showed the chain `api.uploaded → translation.completed → source.connected →
  source.changed → source.drift_detected`, all published;
- `/v1/sources/validate` reported all six stages with durations; a Swagger 2.0 document showed
  `convert: ok {from: swagger2, to: 3.0.3}`; an invalid document **halted at `validate`** with
  `ir_hash: null` and three findings.

### Known limitations, stated rather than implied

- **The URL and GitHub connectors' fetch paths are covered by tests, not by a live external fetch.**
  The SSRF screen refuses loopback (correctly), and no external host was called in this environment.
  Conditional fetching is asserted at the HTTP layer with a faked transport.
- **No webhook endpoint.** The LLD mentions webhook-triggered regeneration; sync is polling-only.
  `WatchPlan` carries a `WATCH_WEBHOOK` mode that nothing returns yet.
- **The sync loop runs in-process**, like the event relay, with the same multi-replica caveat: two
  replicas would both check the same due sources. There is no lease.
- Comparison between two sources is **on demand**, not continuous: `POST /compare` produces a report
  when asked. Nothing schedules it.
- Postman conversion infers types from examples and is lossy by nature. GraphQL requests inside a
  collection are carried over as JSON bodies and flagged; GraphQL ingestion is not implemented.
- Swagger UI discovery probes conventional paths after reading the page. It fetches candidates to
  check they parse, so a page with many referenced URLs costs several requests.
- `api_source` rows backfilled by migration 0035 have **watching off**, whatever the project. Turning
  on outbound polling for existing projects without being asked would be a surprise.

## Phase 21 - ESDS Phase 4: documentation intelligence (2026-08-28)

LLD §3.5 is the part of the platform that reads what a specification cannot say. An OpenAPI document
describes *shapes*; it does not say that refunds are only allowed within 30 days, that a chargeback
is a reversal initiated by the cardholder's bank, or that the refund process is four steps in a
fixed order. Phase 4 extracts those, and — the part that matters — makes every one of them point
back at the sentence it came from.

### The pipeline

    Document → Parse → Chunk → Extract → Graph → Embed

Seven new tables (migration 0036), nine new modules under `server/src/sutr/documentation/`.

**Parsing** produces typed elements with offsets, not a wall of text: `title`, `narrative`, `list`,
`table`, `code`, each carrying its character span and page. PDF, HTML, DOCX, Markdown and plain text.
DOCX is read out of the zip with `zipfile` and `ElementTree` — no `python-docx` dependency for what
is, in the end, XML in an archive. HTML uses the standard library's `HTMLParser`; `bs4` is not a
dependency either. PDF tries `pypdf`, then the `pdftotext` binary, then OCR, and reports which one
ran.

**Chunking** partitions and then groups by heading path. Fixed-size windows cut rules in half —
*"Refunds are allowed only within"* is not a rule — so a chunk is a section, split on sentence
boundaries only when a section is too large to be one. Every chunk carries `Refunds > Eligibility`,
which is what a citation reads.

**Extraction** is deterministic (ADR-032). Deontic markers — *must*, *shall*, *may not*, *is
prohibited* — classified by weighted signals across ten rule types; procedures from numbered lists,
arrow chains and ordinal prose; definitions from a narrow set of defining verbs. Each fact carries
the sentence verbatim, its section path, its page, its offsets, a confidence and the extractor
version.

**Graph** turns those facts into nodes and typed edges, keyed so re-processing updates rather than
duplicates.

**Embedding** is where the phase says no. See below.

### Two decisions worth stating

**Processing runs inline** (ADR-030). The LLD queues it; there is no worker process here. The
alternative was writing a `queued` row and returning immediately with nothing to run it, which is a
worse lie than a slow request — the API would report a state the system cannot leave. The LLD's
*interface* is kept exactly (`POST /v1/documentation/jobs` → `job_id`), and the per-stage
checkpoints are real: `completed_stages_json` is written after each stage, so a re-run resumes. That
is what makes adding a worker a change of caller rather than a rewrite.

**No embedding provider ships, and no substitute is used** (ADR-033). A hash or TF-IDF vector packs,
stores and cosines exactly like a real one, and a search built on it returns confident rankings —
while behaving like lexical search with extra steps. Nobody downstream could tell which they got.
So retrieval is BM25, every response says `"mode": "lexical"` with the reason semantic search is
unavailable, and the code is written for both legs and fuses them with Reciprocal Rank Fusion so
that configuring a provider turns the second leg on without a rewrite.

### Three outcomes, not two

LLD §3.5 states the failure rules directly, and they only make sense with a third state (ADR-031):

- **`processed`** — every stage produced what it should.
- **`partial`** — the document was processed and something was lost, with `degradations` naming what
  and why. Unreadable pages. A graph backend that threw. No embedding provider.
- **`failed`** — parsing failed. A document nobody can read has nothing downstream to do.

In the default install `partial` is the *common* outcome, because no embedding provider is
configured. That is the point: the state says so rather than reporting a clean success over a
document with no vectors.

### API

`POST /v1/documentation/jobs` (the LLD's `{provider_id, document_uri, document_type}` → `job_id`),
`POST /documents` (multipart upload), `GET /jobs/{id}`, `POST /documents/{id}/reprocess`,
`GET/DELETE /documents`, `GET /documents/{id}/chunks`, `GET /rules`, `GET /workflows`,
`GET /glossary`, `GET /graph`, `GET /search`, `GET /capabilities`. New events:
`documentation.uploaded → parsing.completed → knowledge.extracted → embeddings.generated →
documentation.processed`, with `documentation.failed` terminal.

`GET /capabilities` exists because this deployment's abilities are not a constant: PDF parsing needs
a library or a binary, OCR needs Tesseract, semantic search needs a provider. A caller should be
able to find out that a scanned PDF will come back empty *before* uploading it.

### Three bugs found by running it

**Hard-wrapped sentences were invisible to every extractor.** `split_sentences` returned text with
the source's line breaks still in it, so `Refunds are not permitted for digital goods that have\nbeen
downloaded.` matched no pattern — and PDFs and plain text are nothing but hard wrapping. Fixed at
the source: sentences are flattened, and a new whitespace-tolerant `locate()` maps them back to
offsets so citations still point at the original passage.

**`text/plain` was overriding the filename and the content.** Browsers send it for `.md` files and
`application/octet-stream` for anything they cannot place. Parsing Markdown as flat text loses every
heading, which is exactly what the chunker groups by — so a policy document arrived as one
undifferentiated chunk with no glossary section. Generic media types are now weak evidence, and a
document whose head is an ATX heading plus other structure is recognised as Markdown whatever it is
called.

**A flattened table looked exactly like a sentence.** This one only appeared on a real PDF. Running
the 31-page ESDS LLD through the pipeline produced **76 "workflows"** — nearly all of them arrows in
architecture diagrams — and **54 "glossary terms"**, most of which were table cells and
table-of-contents entries. `pdftotext` flattens a two-column page into one line with the gap
preserved, and `Bootstrap: one authoritative source        differences are reconciled` matches a
definition pattern perfectly.

That is not ordinary noise. The whole claim of this service is that every fact cites the sentence it
came from, and a rule citing two cells that happened to land on the same line is a citation nobody
can check — which makes the *checkable* ones less believable too. `extraction/layout.py` now decides
whether a line came out of a layout, on structural signals only: a wide column gap, an arrow between
boxes, a dot leader, a "definition" too short or too capitalised to be prose. All three extractors
consult it (ADR-035).

Result on that document: terms **54 → 16**, workflows **76 → 26**, rules **8 → 4**, with every
survivor a real statement from the text. Nothing extracted from a prose document changed.

It also forced a correction upstream: `split_sentences` had been collapsing *all* whitespace, which
destroyed the column gap before any extractor could see it. It now collapses line breaks only —
enough to make hard-wrapped prohibitions findable, while leaving intact the one signal that says
"this was a table".

### Verification

Suite **1479 → 1568** green (89 new tests); ruff clean; migration 0036 applies, rolls back and
re-applies, and `compare_metadata` reports **0 diffs** between the migration and the models;
contracts regenerated — 39 event schemas, 27 `/v1` paths — and `check_contracts.py` passes.

**Live-verified against a running server** (filesystem storage backend, scratch database):

- a Markdown policy uploaded through `POST /documents` came back `partial` in ~110 ms with 5 rules,
  1 workflow and 3 terms, classified `policy` at 0.825 confidence;
- **every citation offset was checked against the source file** and resolved exactly, including
  across hard line breaks — `chars 180–258` really is `"Refunds are not permitted for\ndigital goods
  that have already been downloaded."`;
- a **real 7 MB, 31-page PDF** parsed via `pdftotext` in 1.4 s: 305 elements, 72,575 characters, and
  page 31 correctly flagged `ocr_unavailable` + `unreadable_pages` — the LLD §3.5 degradation path,
  on a real document rather than a fixture;
- `document_uri: http://127.0.0.1:8123/health` was **refused** — *"The documentation URI was refused:
  Host resolves to a blocked network range"*;
- re-uploading the same bytes returned `deduplicated: true` with the same document id and a new job;
- the blob landed on disk fanned out by prefix with **no `.part` files** left behind;
- the event chain `documentation.uploaded → parsing.completed → knowledge.extracted →
  documentation.processed` was published, with no `embeddings.generated` — because none were;
- a second organisation's document, inserted directly into the database, was **not listed**, returned
  **404** on read and on delete, and **never appeared in search**.

Cross-tenant negative tests (build prompt §61) are explicit: another org's document is **not listed,
reads as 404 rather than 403** (403 would make the endpoint an oracle for which documents another
tenant holds), cannot be deleted or reprocessed, and its rules and chunks never appear in a list or
a search.

### Known limitations, stated rather than implied

- **OCR is NOT TESTED against a real scanned document.** Tesseract is not installed in this
  environment. The unavailable path is tested — pages are flagged and the job continues, which is
  the LLD's rule — but the OCR path itself has been exercised only through that branch.
- **Semantic search is BLOCKED** on an embedding provider (ADR-033). The hybrid fusion path is
  tested with a stub provider that lives in the test file, where it cannot be mistaken for something
  shipped.
- **Extraction is English-only and reads one sentence at a time**, so a rule spread over two
  sentences is missed. Stated in the module rather than discovered by a user.
- **Workflow steps are not yet linked to operations or tools.** The columns exist; populating them
  needs the IR and the document knowledge in the same place, which is Phase 5.
- **The knowledge graph is in the relational store**, not Neo4j (ADR-015). Entity recognition inside
  rule text is keyword-based, so it is a useful index, not an ontology.
- **No object storage** (ADR-034). Originals live in the database by default, or on the filesystem;
  an S3/MinIO backend is refused with `NOT_CONFIGURED / DOCUMENTATION_REQUIRED` rather than written
  against an API this repository has not exercised.
- **Extraction is not parallel** (LLD §3.5 asks for it). The rule-based extractor is regex over text
  and is not the bottleneck; parallelism belongs with the worker and a model-based extractor.

## Phase 22 - ESDS Phase 5: MCP generation and the runtime it produces (2026-08-28)

LLD §3.6 is the point where a specification becomes something that runs. Sutr
already generated a package; what it did not do was keep it, check it, or refuse
to deploy one that failed a check. This phase built the artifact.

    knowledge → metadata → generate → validate → sign → store

`server/src/sutr/generation/` — nine modules: `knowledge`, `manifest`, `sbom`,
`scanning`, `signing`, `validation`, `artifacts`, `events`, `pipeline` — plus
`POST /v1/generation/runtimes`, the LLD's own interface.

### Immutable means immutable, and the build hash is what enforces it

The LLD asks for *"immutable artifacts · identical inputs ⇒ identical outputs"*.
Both halves are easy to claim and easy to break by accident: one embedded
timestamp and determinism is gone, one `updated_at` column and immutability is.

`runtime_artifact` has no update path — no route, no service function — and
`build_hash` covers the inputs (project, IR version and hash, knowledge hash,
runtime, template version) together with the digests of the manifest and the
package. Storage is keyed on it, so generating again from unchanged inputs
returns the artifact you already have.

That makes the determinism claim **self-enforcing** (ADR-036). A
non-deterministic packager would produce different bytes, a different hash, and
a second row — which is exactly what the test asserts against. Determinism
stops being a promise in a docstring and becomes something that fails the build
when it stops being true.

The validation report is deliberately outside the hash: it records durations and
depends on which scanners the install has configured, and a build that took
longer is not a different build.

### `blocked` is not `ok`

The LLD's validation list mixes checks that need only the package with checks
that need a vulnerability database and a scanner this repository must not
invent (build prompt §4). Two obvious answers are both wrong. Marking an
un-runnable check as passed tells a reader the artifact was scanned when it was
not; failing every artifact on an install with no scanner means nothing ever
deploys.

So there are four states (ADR-037). `ok` ran and found nothing. `failed` ran and
found something — the artifact is rejected. **`blocked` could not run**, is
named in the report and in the summary sentence, and never counts as a pass.
`skipped` does not apply.

Seven checks. Five are implemented and real: `compile` compiles every generated
file, `mcp_compliance` checks names, descriptions and schemas against what
mainstream MCP clients accept, `dependencies` refuses a requirement from a URL
or without a version constraint, `static_analysis` is an AST walk for `eval`,
`os.system`, `pickle.loads`, `subprocess(..., shell=True)` — described as that,
not as SAST — and `secrets` looks for credential-shaped values in generated
code. `tests` runs the package's own offline suite in a subprocess. The two
scans are commands the operator names, because the scanner they trust is theirs
to choose and parsing a specific vendor's JSON would mean guessing at a schema
this code has never seen.

And the gate is enforced where it matters: `POST /api/deployments` takes an
`artifact_id`, refuses a rejected one with 409 and the name of the check that
objected, and deploys the exact bytes that passed.

### Signed, and precisely about what

The LLD asks for signed images via Cosign. There is no image build here and no
registry to push to, so there is nothing for Cosign to sign, and wiring Sigstore
against a registry this code has never contacted would be inventing the part
that matters.

What ships instead is an **Ed25519 signature over the artifact's build hash**,
made with a key the operator holds — a real cryptographic assertion, verifiable
offline. It is not Sigstore: no transparency log, no certificate chain, no
keyless identity, and it does not sign a container image. ADR-038 says so, and
the traceability matrix records §3.6.7 as PARTIAL rather than met.

With no key configured, artifacts are unsigned and every read says so. A
placeholder signature was rejected outright: a reader seeing a `signature` field
would reasonably believe it meant something.

The SBOM is CycloneDX 1.5, covering both the files the package ships (each with
its digest) and the dependencies it declares. One limitation is written into the
document itself rather than left to be discovered: dependency entries carry the
**declared constraint, not a resolved version**, because the package pins ranges
and the index is never contacted. An empty `version` with the constraint in a
property is the honest shape.

### The generator finally has its second input

LLD §3.6 defines the input as *IR + documentation knowledge + template*. Sutr
had the first and the third; Phase 4 produced the second and left it in the
control plane.

Now the rules, glossary and workflows extracted for a project are written into
the generated `tools.json`, citations intact. A generated server is usually run
somewhere the platform cannot reach, by an agent that will never call the
retrieval API — a rule that exists only in the control plane is a rule that
agent will not follow.

Two limits are deliberate (ADR-039). The attachment of a rule to a tool is a
**match, not a claim**: token overlap, nothing inferred, nothing generated, and
a rule that matches no tool is still shipped at the top level. And the volume is
capped, with the truncation counted, so a provider with a thousand rules does
not silently turn a 60 KB package into a megabyte of prose.

The same matching closes §3.5.6, which Phase 4 left open: a documented
workflow's steps are linked to the operations they name. It lives in
`documentation/linking.py`, not in the generator, because `doc_workflow` is a
documentation table and one service writes a table. **An unmatched step stays
unmatched** — attaching it to the nearest-looking operation would produce a
wrong link that reads exactly like a verified one.

### The generated server got the rest of its middleware

Template version 2. The chain the LLD names — Authentication → Validation →
Logging → Execution → Metrics → Response — is now all six.

- **Validation** checks arguments against the declared schema before anything
  leaves the process. Unknown arguments are ignored rather than refused: they
  have no effect on the request, and rejecting a call over one would be worse.
- **The connector** has per-phase timeouts (nothing unbounded), bounded retries
  on **idempotent methods only** — replaying a POST after a read timeout can
  charge a card twice — and a **per-tool circuit breaker** with a real half-open
  state that admits one probe. A 4xx does not count towards it: one agent's bad
  arguments must not cut off every other caller.
- **Error translation** turns a status into something actionable. An agent
  reading "HTTP 429" often retries immediately; reading that it is rate limited,
  it waits. The upstream body is kept — the hint is added, never substituted.
- **Metrics** are in-process and stdlib-only, exposed at `/metrics` in the
  Prometheus text format. A generated server is a container the provider runs
  wherever they like; making it depend on a metrics backend would make it depend
  on infrastructure the platform cannot see.
- **Logging** is one structured line per call on stderr — stdout carries the MCP
  protocol on the stdio transport, so a log line there would corrupt the session.

The image is non-root with an unwritable application directory. It is
deliberately **not** digest-pinned: pinning to a digest this generator has never
pulled would assert a provenance it cannot back.

### Runtime isolation, and what it is honest about

LLD §4.3.8 asks for things only Kubernetes has: a namespace per provider,
NetworkPolicy, ServiceAccount, resource quotas, Pod Security Standards. The new
`deploy/kubernetes_provider.py` creates all of them — PSS enforced at
`restricted`, a default-deny NetworkPolicy plus two narrow allowances (DNS, and
egress to public addresses with every private range excluded, which is what
stops a runtime reaching other workloads), a ServiceAccount with no token
mounted, a ResourceQuota, and a non-root container with a read-only root
filesystem and all capabilities dropped. The credential goes into a Secret, not
the pod spec.

Two things it does not pretend about (ADR-040). There is no build worker and no
registry, so the package is mounted from a per-revision immutable ConfigMap over
a base image the operator names — a real mechanism with a real ~900 KB limit,
checked before the request rather than discovered at apply time. And no Ingress
is created: the hostname, ingress class and TLS belong to the cluster, and a
guessed URL would be one that does not resolve.

**It is NOT TESTED against a live cluster.** Every request is built against the
documented Kubernetes API and asserted against a stub API server — 40 tests on
paths, methods and bodies. Nothing has run against a real one, and the matrix
says NOT TESTED until it has.

### Drift, and the word GitOps

LLD §5.7 wants drift detection, version history, one-command rollback and an
audit trail. Three existed. `deploy/drift.py` and
`GET /api/deployments/{id}/drift` are the fourth: what the provider actually has
against what the platform declared — the live revision, the package digest,
whether it is up.

This is **not GitOps** (ADR-041), and the matrix says so rather than blurring it.
The desired state is the deployment row, not a Git repository, and the
comparison runs when someone asks rather than continuously. The comparison is
also deliberately narrow — only facts the platform declared — because a
provider always reports fields Sutr never asked for, and a detector that is
never clean is a detector nobody reads. "Could not check" is a distinct answer
from "nothing wrong".

### Verification

Suite **1568 → 1732** green (164 new tests); ruff clean; migration 0037 applies,
rolls back and re-applies, with `compare_metadata` reporting **0 diffs** for the
new table and columns; contracts regenerated — 39 event schemas, 34 `/v1` paths
— and `check_contracts.py` passes.

**Live-verified against a running server** (scratch database, real Docker
daemon):

- generation took ~640 ms end to end, of which 602 ms was validation actually
  running the generated package's own pytest suite — which passed;
- the report read *"Validated, with security_scan, vulnerability_scan not run on
  this install"*, naming what did not happen rather than implying it did;
- **generating a second time returned the same artifact** with `reused: true`,
  a `validate` stage marked `reused`, and still exactly one row stored;
- the downloaded zip hashed to the recorded `package_sha256`, and its shipped
  test suite passed again outside the platform (7 tests);
- the generated server **started, served `/health`, and served `/metrics`** in
  Prometheus text format at `text/plain; version=0.0.4`;
- driving its middleware chain against an unreachable upstream: a malformed call
  was refused by Validation before the network, five transport failures each
  retried twice (**10 retries**), and the sixth call was **refused locally by an
  open circuit breaker** — with all of it in the metrics snapshot;
- a **real Docker deployment from the artifact** came up `running`, recorded its
  `artifact_id`, injected the credential as `PETSTORE_API_TOKEN` — the variable
  the *artifact's manifest* declares, not one derived from the deployment name —
  and reported **no drift**;
- marking that artifact `rejected` made the same deploy return **409**: *"did not
  pass validation (security_scan) and cannot be deployed"*;
- with a signing key configured, an artifact came back **signed and verified**
  (`ed25519`, key id `live-verification`, `package_intact: true`);
- with a vulnerability-scan command that deliberately exits non-zero, the
  artifact was **rejected, stored anyway, readable, and refused at deploy** —
  validation failing is a finding, not a lost build;
- the event chain `metadata.generated → generation.started → mcp.generated →
  validation.completed` was published for all three runs, each keyed on its own
  `generation_id`, with the last one carrying `signed: true` and
  `validated: false, failed_checks: ["vulnerability_scan"]`;
- a second organisation could not list, read, download or deploy the first
  organisation's artifact: **404 on every route**, never 403.

### Known limitations, stated rather than implied

- **The Kubernetes provider has never run against a live cluster.** Its objects
  are asserted against a stub API server. That is a design review, not a
  deployment.
- **Vulnerability and security scanning are `blocked` by default.** No scanner
  and no CVE database ship. An operator supplies a command; without one the
  report says so on every read.
- **Signing is not Cosign and does not sign an image.** No transparency log, no
  certificate chain, no keyless identity (ADR-038).
- **SBOM dependency versions are unresolved.** Ranges, not a lock file, because
  resolving them needs a build that installs them — the isolated build worker
  the LLD asks for and this install does not have.
- **Go and Node templates do not exist.** They are declared and refused by name
  rather than silently absent.
- **Integration and smoke tests against a provider's live API are not run.**
  They would mean calling somebody else's production API with a real credential
  as a side effect of a build.
- **A package over ~900 KB cannot be deployed to Kubernetes** by this provider,
  because a ConfigMap cannot carry one. Refused up front, with the limit named.
- **Drift detection is not reconciliation and not GitOps.** Argo CD is not
  wired, and the matrix keeps §5.7.1 at PARTIAL.
- **No frontend.** LLD §3.6 and the build prompt's deliverable list enumerate
  server capabilities; no console page was added for artifacts.

## Phase 23 - ESDS Phase 6: the registry, and a storefront derived from it (2026-08-28)

LLD §3.7 is two sentences that decide an architecture:

> Registry = authoritative system of record (metadata, versions, runtime refs,
> governance state). Marketplace = read-optimized storefront **derived from
> Registry events**.

The easy implementation is one service with two names. This is not that.

    registry  →  events  →  projection  →  storefront

`server/src/sutr/registry/` (lifecycle, versions, pricing, trust, service,
events) and `server/src/sutr/marketplace/` (projection, listings,
subscriptions, profiles, events), seven tables in migration 0038, and two API
surfaces: `/v1/registry` for a provider's own record, `/v1/marketplace` for the
cross-tenant storefront.

### One lifecycle, and it has fourteen states

The LLD describes the lifecycle twice: fourteen states in §3.1, labelled *"the
shared vocabulary for the whole platform"*, and a five-state summary in §3.7.
Implementing both would produce two enumerations to map onto each other and one
to forget when the other changes, so there is one, and it is §3.1's (ADR-042).
The matrix's earlier "10-state" note was a miscount and is corrected.

The six failure states from §4.1 branch off it and **resume at the stage that
failed** — `TRANSLATION_FAILED` goes back to `TRANSLATING`, not to `DRAFT`,
because sending a provider to the beginning discards every stage that worked.
Two asymmetries are deliberate: a pipeline failure *can* be abandoned back to
DRAFT (a provider who decided the spec was wrong should not have to fake a fix
to escape), and a `SUSPENDED` tool cannot, because it has subscribers.

`GET /v1/registry/lifecycle` reports the whole machine, so no client has to
hard-code it, and a refused transition names both ends and what *is* possible.

### The storefront is a real projection

`marketplace_listing` is written only by event handlers, consuming
`tool.published`, `tool.updated`, `version.created`, `pricing.updated`,
`tool.deprecated`, `tool.archived` and `subscription.created`. It is the first
consumer of the event machinery built in Phase 2 — machinery that existed with
nothing using it, which is a good way for machinery to be quietly wrong.

Each handler **rebuilds the listing from the registry record** and uses the
event only as the signal that something moved. Patching from the payload would
make every event carry a full copy of the tool, and the two would disagree the
first time somebody added a field to one and not the other.

Being a projection has a consequence, so the consequence is on every row:
`projected_at` and `projected_from` say which event produced it, and the
storefront response says listings can lag. **Nothing authorizes off a listing** —
subscribing re-reads the registry, so a tool archived two events ago cannot be
subscribed to because the storefront has not caught up (ADR-043).

Two projection rules are decisions rather than mechanics. **Deprecation does not
delist**: a tool nobody should start using must not vanish from under the people
already using it, so the listing stays and carries the note; archiving delists.
And **subscriber counts are counted, never incremented** — an increment drifts
the first time an event is replayed or a subscription is cancelled behind the
projection's back.

### A trust score you can argue with

Seven inputs, exactly the ones §3.7 names. Weights summing to 100 so each reads
as "up to this many points" and the arithmetic is checkable by eye. Every
component reports the evidence it used.

The part that took the thinking is what to do about inputs that cannot be
measured — which, for a new tool, is most of them. Scoring an unmeasured input
as zero punishes a tool for being new; scoring it as one flatters it. So an
unavailable component **drops out of both the numerator and the denominator**,
its absence is named, and the score carries a `coverage` figure saying how much
of it was actually measured (ADR-044).

Live, a freshly registered tool scored:

> `42/100 from governance_status 5/10, doc_quality 3/10. Not measured:
> security_scans, validation_success, runtime_availability, error_rate,
> user_ratings.` — coverage 0.2

That is a more useful sentence than "42". A reader can tell 90-at-30%-coverage
from 90-at-full-coverage, which is the difference between a promising tool and a
proven one. With nothing measurable at all the score is **null with a reason,
never 0** — a tool nothing is known about is not a tool known to be bad.

Phase 5's fourth validation state pays off here: a `blocked` security scan makes
the component abstain rather than score zero, because an install with no scanner
has not established that a tool is clean.

### Governance: the gate, not the engine

Four changes are gated — entering review, publishing, changing visibility,
changing price — and each produces a change request that **changes nothing**
until somebody decides it. Everything else applies immediately, because a gate
on `TRANSLATING → IR_READY` would be a human approving a parser.

A change request records both sides of the change: an approver deciding from the
request alone is deciding without knowing what it replaces. A price is validated
*when requested*, so an approver never discovers the price was malformed.

Four eyes where four eyes are possible: a requester may not decide their own
change when the organization has another eligible approver. Enforcing it
unconditionally would leave a solo install unable to publish anything, which is
a deadlock rather than governance — so a self-decision is allowed there and
recorded as `self_decided`, which is what an audit trail is for (ADR-045).

The policy *engine* that could decide some of these automatically is Phase 9,
and the matrix says 3.7.4 is PARTIAL for exactly that reason.

### Pricing is history, and nothing is billed

A price is never edited: changing it supersedes the live row and writes a new
one. A subscription snapshots the price **by id**, so a provider raising their
price does not silently reprice everyone who already subscribed. Amounts are
integer micro-units, because a price stored as a float is eventually off by a
hundredth somewhere it matters.

Nothing here charges anybody, and every serialized price and subscription says
so with `billed_by_this_platform: false` (ADR-046). A tool nobody has priced
serializes as **null, not free** — "nobody set a price" and "the provider chose
to charge nothing" are different facts.

### The older catalog stopped lying about why

`/api/marketplace` has been returning trust score, pricing, versions, regions
and compliance as explicit nulls with the reason *"the Registry service is not
implemented"*. It is now, so the reason changed to the accurate one — this
integration has no registry record — and a registered integration carries the
real values. Live: `posthog` before registration returned five pending fields;
after registering it with an `integration_id`, the same call returned trust 61,
its declared regions and compliance, and nothing pending.

### Verification

Suite **1732 → 1855** green (123 new tests); ruff clean; migration 0038 applies,
rolls back and re-applies with `compare_metadata` reporting **0 diffs** across
all seven new tables; contracts regenerated — 39 event schemas, 58 `/v1` paths —
and `check_contracts.py` passes.

**Live-verified against a running server**, two tenants, with the relay running
rather than drained by hand:

- a tool asked to jump `DRAFT → PUBLISHED` was refused with *"A tool in DRAFT
  cannot move to PUBLISHED. It can move to: API_UPLOADED."*;
- the nine ungated transitions applied immediately; `DEPLOYED → UNDER_REVIEW`
  came back `applied: false` with the tool **still DEPLOYED** and a change
  request id;
- deciding the two gates published the tool, and the **listing appeared in the
  storefront by itself** — trust 67, `projected_from: tool.published`;
- an unpriced listing showed `pricing: null`, not a free badge;
- a price change was invisible until approved, then reached the listing with
  `projected_from: pricing.updated` and `billed_by_this_platform: false`; a
  zero-amount `per_call` price was refused **at request time**;
- a second tenant subscribed (`subscribed`, not entitled), provisioned
  (`active`, entitled, renewing in 30 days), and the provider saw one subscriber;
- **deprecation kept the listing** with its note, and refused a new subscriber
  while the existing one stayed entitled; **archiving delisted it** and the
  detail route went to 404;
- the consumer got **404 on every one of the provider's registry routes** —
  read, trust, versions, pricing and transition — and saw zero tools of their own;
- the full event set was published: `tool.registered`, `tool.updated`,
  `version.created`, `tool.approved`, `tool.published`, `subscription.created`,
  `pricing.updated`, `tool.deprecated`, `tool.archived`, plus `review.created`
  and `rating.updated` from a review.

### Known limitations, stated rather than implied

- **The governance gate has no policy engine.** Every gated change waits for a
  person. Rules that decide automatically are Phase 9.
- **Nothing is billed.** Pricing and subscriptions are metadata a billing
  service would read; no money moves, and every response says so.
- **Provisioning is bookkeeping.** It records that access was arranged; this
  platform does not arrange it as a side effect of a subscription, and claiming
  otherwise would be the fake completion §83 forbids.
- **Trust does not yet influence discovery ranking.** It sorts the storefront;
  the ranking function §3.8 describes is Phase 7.
- **Subscription expiry is a sweep with no scheduler.** `expire_due` exists and
  is tested; nothing calls it on a timer yet.
- **`regions` and `compliance` are provider declarations.** Nothing verifies
  them, and every response that carries them says so in `declared_by_provider`.
- **Provider verification is manual.** `verified` is platform-granted and has no
  route; it is set in the database or by a future admin surface.
- **No frontend.** The registry and the new storefront have no console pages;
  the existing `/app/marketplace` still renders the integration catalog.

## Phase 24 - ESDS Phase 7: discovery (2026-08-31)

LLD §3.8 turns an intent into a ranked, policy-filtered recommendation:

    intent → candidates → policy filter → hybrid retrieval → ranking → cache

`server/src/sutr/discovery/` — corpus, policy, retrieval, ranking, cache,
quality, service — plus `POST /v1/discovery/search`, `/evaluate` and
`/capabilities`. **No migration:** discovery writes nothing, which is the
structural form of *"read-optimized and latency-critical; it never invokes
provider APIs"*.

### The filter runs first, and its rejections are the answer

The LLD says the eight policy checks run *before* ranking, and it is worth
saying why that ordering is not arbitrary. Ranking is the expensive half of the
request; ranking a tool the caller may not use spends it on an answer that was
never available. And a ranker that never sees an inaccessible candidate cannot
leak its existence through a score.

The second half of the decision came from §4.2's hot-path table, which says a
request with no match should return *"suggestions or 'tool not found'"*. An
agent handed an empty list learns nothing. So exclusions are **kept**, each with
the check that produced it, and when nothing survives the near misses come back
ranked by how well they matched (ADR-047). Live:

> `refunds-api — subscription: You are not subscribed to this tool.`
> `shipping-api — region: The provider declares this tool for us-east-1, not eu-west-1.`

Six of the eight checks are enforced. Two — `provider_policy` and
`runtime_status` — have nothing to read in this build, and `capabilities`
reports them `enforced: false` rather than letting a check that never excludes
anything look like one that ran.

### A ranking bug that only running it would find

Hybrid retrieval fuses rankers by Reciprocal Rank Fusion, which uses only the
*rank* each candidate reached — the right tool when a BM25 score and a cosine
similarity have no common unit.

Applied to a **single** ranker it is actively wrong, and the live run showed it.
RRF turns first and second place into 1/61 and 1/62 — two thousandths apart —
so `refund a customer payment` came back with `refunds-api` at 1.284 and
`invoices-api`, which matched only the word "payment", at **1.268**. Relevance
had stopped discriminating and trust was deciding the order.

Fusion now uses the single ranker's own normalised scores when only one
contributed (ADR-048). The same query afterwards: **1.284 and 0.460**. This is
the common path, not a corner — no embedding provider ships, so a default
install has exactly one contributing ranker.

The related fix was morphology. BM25 with no plural folding treats `refund` and
`refunds` as unrelated terms, which on short noun-heavy tool descriptions was
the largest single source of wrong rankings. `common/lexical.py` now folds
English plurals — and only plurals, because a real stemmer conflates words that
mean different things (ADR-049). Documentation search got the same improvement,
and its suite passed unchanged: there is now **one** BM25 rather than two that
would drift.

### Four degradations, four tests

| the LLD's row     | what happens                                      |
|-------------------|---------------------------------------------------|
| Graph DB down     | expansion skipped, request continues              |
| Embedding failure | keyword-only for this request                     |
| Ranking timeout   | fall back to `relevance-only-1` — retrieval order |
| Index lag         | serve what is indexed, flag the stale rows        |

Each is a **named** degradation on the response rather than a silent difference,
and each has its own test (build prompt §79). Index lag is detected rather than
assumed: a listing whose `projected_at` predates its registry record's
`updated_at` is served and flagged. Live, editing a record without letting the
projection catch up produced exactly that — `stale: true` on the row, and *"1
listing(s) were projected before their registry record last changed"*.

The budget is **cooperative**: elapsed time is checked between stages, not
enforced by preemption, so a stage that blocks still blocks. `capabilities` says
that in words rather than letting "budget" imply something stronger (ADR-051).
The 500 ms constant is the point at which ranking is abandoned — a property of
the code, testable — not a latency claim, which remains NOT TESTED.

### Ranking you can argue with

Three named versions (`balanced-1`, `trust-first-1`, `relevance-only-1`) over
six signals, selectable per request. Every result carries its arithmetic:

> `1.438 = relevance +1.000 trust +0.234 entitlement +0.150 freshness +0.050 adoption +0.004`

A **missing trust score is neutral, not zero**. Phase 6 was careful to make an
unmeasurable score `null`; ranking it as 0 would have undone that in the one
place it is most visible, so an unscored tool ranks as if trust did not apply to
it and the explanation says the signal was absent.

### The cache, and what it honestly is

Keyed on tenant + intent + policy version, as the LLD says, plus the ranking
version and the caller's requirements — both change the answer. Invalidation is
by **generation counter** off registry events, not deletion: knowing which
intents a tool could have matched is the search problem again. A publish or an
archive bumps a *global* counter, because the storefront is cross-tenant.

It is in-process, so per replica. A shared cache is Redis and none is wired in;
`capabilities` says so rather than letting an operator assume a hit rate they
are not getting (ADR-050).

### Quality metrics with nothing invented to measure

Precision, recall and NDCG are implemented. **No relevance judgements ship**, and
none were invented — `POST /v1/discovery/evaluate` scores against judgements the
caller supplies, runs uncached queries, and says in the response that the numbers
describe that set and nothing else (ADR-052). Recall is `null` rather than 1.0
when a judgement lists nothing relevant.

Live, two judgements returned `precision 0.25 | recall 0.50 | ndcg 0.50`, with
the per-query breakdown showing which intent missed. That is a more useful thing
to have than a dashboard of numbers measuring a fiction.

### Verification

Suite **1855 → 1926** green (71 new tests); ruff clean; contracts regenerated —
39 event schemas, 62 `/v1` paths — and `check_contracts.py` passes. No
migration.

**Live-verified against a running server**, two tenants, three published tools:

- `refund a customer payment` ranked `refunds-api` first with
  `terms ['payment', 'refund']` and the full contribution breakdown;
- the response named its degradation — `vector`, *"ranked on keyword matching
  alone"* — with the `NOT_CONFIGURED` reason;
- the stage record showed `candidates → policy → retrieval → ranking`, filter
  before rank;
- `entitled_only` returned **no results and two suggestions**, each naming the
  subscription check; subscribing and provisioning made the tool the top result,
  now with an `entitlement` contribution;
- a region the provider does not declare excluded the tool with the reason
  quoting both regions;
- the second identical search came back `cached: true`, and
  `POST /cache/invalidate` made the next one `false`;
- a provider's unpublished draft was found by **the provider and by nobody
  else**;
- editing a registry record without letting the projection catch up produced a
  result flagged `stale` with the index-lag degradation.

### Known limitations, stated rather than implied

- **Vector retrieval needs a provider and none ships.** The default install is
  keyword-plus-graph, and every response says which rankers contributed.
- **Two policy checks are declared and unenforced**: `provider_policy` (no
  per-tool policy exists) and `runtime_status` (a registry tool is not linked to
  a runtime). Both are reported `enforced: false`.
- **The cache is per replica**, and "runtime unavailable" has no event to listen
  for — that invalidation is the manual endpoint.
- **Quality is measured on request, not tracked.** There is no store for
  judgements or historical scores, so nothing trends over time.
- **The latency budget is cooperative**, and the <500 ms target remains NOT
  TESTED as a latency claim.
- **The corpus is registry tools.** The bundled-integration catalog keeps its own
  search; it has no lifecycle or subscription for the policy filter to read.
- **No frontend.** Discovery has no console page.

## Phase 25 - ESDS Phase 8: provisioning and zero trust (2026-08-31)

LLD §4.3 opens with a claim the rest of the section has to earn: *"No implicit
trust for anyone or anything. Every user, agent, microservice, and runtime has a
unique identity."* Sutr had users, orgs and API keys — a credential with a label
— and an authorization model that was one role matrix.

    principal → four ordered layers → decision → scoped pass

`server/src/sutr/provisioning/` (identity, rules, pdp, passes), three tables in
migration 0039, `POST /v1/provisioning/...`, a Vault secrets backend, and the
authorization layers wired into the tool-execution gate that both the REST and
MCP surfaces already call.

### The figure that could not be read

§4.3 shows *"AUTHORIZATION — four layers, evaluated in order"* as an **image**.
In the condensed edition the text layer carries the caption and the ABAC
example and nothing else: the four box labels are not extractable.

Guessing four names and presenting them as the LLD's would be fabrication of
exactly the kind §4 forbids. Implementing nothing because one figure would not
copy-paste would leave the platform with no ABAC at all. So the four layers are
**derived from what the document says in text** — it names tenant isolation,
RBAC, ABAC and per-tool policy — and the code, the API and the matrix all say
that the derivation is a derivation (ADR-053). `GET /v1/provisioning/capabilities`
returns a `source` field saying so.

    1. tenant   is the principal acting inside its own tenant?
    2. rbac     does the caller's role hold the permission?
    3. abac     do the attributes satisfy this tenant's rules?
    4. policy   the per-tool execution setting and approval requirement

The order earns its keep: each layer is cheaper than the next and refuses on
less information, so a cross-tenant caller never causes a tenant's rule set to
load. **Layers that did not run are absent** from the decision rather than
recorded as having agreed. Layer 4 is delegated to the existing
`approvals/policy.py`, which returns something richer than allow/deny, and
`describe()` says which layer is decided where instead of hiding the split.

### An identity is not a credential

An API key is a credential with a label. You cannot scope a pass to "the finance
agent" if there is no finance agent — only a key somebody named *finance*.

`agent_identity` is separate from the key that authenticates it, carries the
tenant's attributes, and outlives key rotation. One key binds to at most one
identity; a credential speaking for two would make every call ambiguous.
Principals get one comparable form, including the honest one: an API key with
no bound identity is `sutr:api-key:…`, an actor with no identity rather than an
agent it is not (ADR-054).

Live, the same key answered `whoami` three ways as its binding changed:
`api-key` → `agent finance-agent` with `{role: finance, region: india}` →
`api-key` again after the identity was revoked.

### A pass is the record of a decision

The LLD gives five adjectives — *short-lived, single-purpose, least-privilege,
signed, revocable* — and one ordering: *"Issued by Provisioning after the policy
decision."* Each is enforced rather than described.

`aud` **is** the resource, so a pass minted for one deployment is rejected by
another — by the verifier, not by intention. Requested tools are intersected
with what the decision allowed, so asking for more **narrows** the pass instead
of widening it or being refused. Issuance takes a `Decision` and raises on a
refused one: a pass without a decision behind it would make the audit trail
describe something that did not happen.

The JWT shape was not invented here. The generated MCP servers have validated it
since Phase 5 (`GOVERNANCE_MODE=platform`), so the issuer had to match them
rather than the reverse.

**The token is never stored.** The row holds the claims, the decision that
produced them, and what is needed to revoke it. A table of live bearer tokens is
a table whose compromise equals compromising every agent at once (ADR-055).

And the half-truth this phase refused to tell: **a generated runtime cannot see
a revocation.** It validates offline, which is what lets it keep serving when
the control plane is down (LLD §2.2). Revocation is enforced where the platform
is in the path, the short lifetime bounds the gap, and both the capabilities
response and the revoke response say so in words.

### Deny wins; no rule is no opinion

An ABAC layer added to a platform that already runs is added to tenants who have
written no rules. Default-deny breaks every existing call on the day it ships;
priority-ordered allow lets a stray rule overrule a deliberate deny.

So: **deny wins unconditionally** — priority orders which rule is *reported*,
never the outcome — and **a rule set with no matching rule has no opinion**, so a
tenant with no rules keeps the behaviour they had (ADR-056). Both sides of every
comparison are lowercased on the way in, because a rule saying `"India"` against
an identity saying `"india"` is a rule that silently never matches, which is the
worst failure an authorization layer can have.

Two authoring guards exist for that same reason: a rule with no matchers at all
is refused, and a misspelled resource attribute is refused **at write time**.

### A bug the live run found

A matcher with an **empty string** value — `{"integration_id": ""}` — was
accepted and matched nothing. It slipped through because normalisation dropped
`None` but kept `""`, and validation ran on the raw dicts rather than the
normalised ones. Exactly the failure the guards above were written to prevent,
one type short.

Empty values are now dropped during normalisation and validation runs on the
normalised form, so such a rule either becomes a readable broader rule or is
refused outright.

### Vault stores secrets; it does not mint them here

*"Runtime → secret reference → Vault → temporary credential → Provider API"* is
two features. Storing and reading a secret in Vault is one, and it is
implemented against the documented KV v2 API. Minting a short-lived provider
credential from a dynamic secrets engine is the other, and it needs an engine, a
role and a lease policy **per provider** that no document specifies — so
`secrets.describe()` reports `dynamic_credentials.available: false` with the
reason rather than letting a Vault backend imply credentials it does not mint
(ADR-057).

That required a real schema change: `secret` now carries a `ref`. With an
external store the row holds a **pointer**, which is what §4.3.6 actually asks
for and what the db and db_kms backends structurally cannot provide.

### Verification

Suite **1927 → 2020** green (93 new tests); ruff clean; migration 0039 applies,
rolls back and re-applies with `compare_metadata` reporting **0 diffs**;
contracts regenerated — 39 event schemas, 73 `/v1` paths — and in sync.

A new suite that belongs to no phase: `test_tenant_isolation.py` walks **every
tenant-scoped `/v1` surface** and asserts two properties for each — another
tenant's rows are not listed, and reading one is **404 rather than 403**, because
a 403 confirms the resource exists and turns an authorization boundary into an
existence oracle. A resource added later without org-scoping fails here.

**Live-verified against a running server:**

- the LLD's own rule — `Finance ∧ Region=India ⇒ Allow Refund Tool` — written,
  and the decision came back with every layer explained, including
  `rbac abstain: This principal carries no role — API keys and services are not
  role-checked. Use an access rule to constrain it.`;
- a pass asking for two tools came back covering **one**, with
  `narrowed_from` and *"Least privilege: tools you are not entitled to were
  removed rather than the request being refused"*;
- a deny rule at a **lower** priority than the allow rule still won, and the
  refusal named it: `abac: Refused by access rule: payment-freeze`;
- re-reading an issued pass never returned the token again;
- a **real tool call** through `/api/tools/{id}/call` was refused at the gate,
  and the log line carried `abac: Refused by access rule:
  finance-cannot-touch-pets-2` — LLD §4.2's *"403 + policy reason"*, with the
  reason;
- revoking the identity revoked its live pass in the same request, and the bound
  key went back to being an unidentified `api-key` principal.

### Known limitations, stated rather than implied

- **The four layer names are derived, not quoted.** The LLD's figure is an image
  the condensed edition does not carry text for, and the API says so.
- **Revocation does not reach a deployed runtime.** It validates offline by
  design; the short pass lifetime is the bound, and both endpoints say it.
- **Nonce enforcement is per process**, so a pass could be replayed once per
  replica within its lifetime.
- **Vault is NOT TESTED against a live server**, and dynamic credentials are not
  implemented at all.
- **The default install still stores credentials in the database** and injects
  them as environment variables — §4.3.6's deviation, reported on every read
  rather than hidden.
- **No mTLS** between services, so 4.3.7 and 4.3.15 stay partial.
- **The platform's own session JWTs are unchanged**: long-lived, no nonce. Only
  access passes are short-lived.
- **A call with no principal skips layers 1–3.** That is what made the layer
  safe to add to a hot path hundreds of tests already cover, and it means an
  internal caller that does not resolve a principal is not authorized by these
  layers.
- **No frontend.** Identities, rules and passes have no console page.

## Phase 26 - ESDS Phase 9: governance, compliance and policy (2026-08-31)

LLD §5.2 asks for continuous governance across seven points, versioned policies
with a seven-state lifecycle, two engines, an approval workflow, exceptions that
expire, separation of duties, and four fail-safe behaviours — under one rule:
*"Governance failures never silently permit unauthorized actions."*

`server/src/sutr/governance/` — policies, compliance, risk, review, exceptions,
engine — six tables in migration 0040, and `/v1/governance` with the three
endpoints §5.2.13 names plus the ones the engines need.

### Writing a policy is not deploying one

A policy is the identity; every statement it makes lives in a **version**, and a
version stops being editable the moment it leaves DRAFT. Everything downstream —
a review decision, an activation, an audit record — refers to *what it said*, so
a version editable after approval makes the approval a statement about nothing.

`PUBLISHED` and `ACTIVE` are separate states because they are separate facts: a
version can be released without being the one enforced, and exactly one is
active. That is what makes §5.2.11's *"roll back to last active version"* a
single pointer move rather than a reconciliation.

Only an **ACTIVE** version's rules reach the decision point, alongside the
standalone access rules from Phase 8. Deny wins across both, and every match
reports its source — so a refusal says which document to go and read:

> `matched: no-refunds-outside-finance | source: policy | policy: refund-controls v1`

### Separation of duties, stricter here than in the registry

Phase 6's registry gate enforces four eyes *only when a second approver exists*,
because a solo install would otherwise be unable to publish a tool at all.
Policy approval refuses **unconditionally** (ADR-058), because the reason for
that exemption does not apply: a tenant with no policies loses nothing, since a
policy with no active version contributes no rules.

Live, the author trying to approve their own version got:

> *"You wrote this policy version, so you cannot approve it. Separation of
> duties (LLD §5.2.10) is unconditional for policies: another administrator has
> to approve it. Add one, or have an existing one review it."*

An unconditional rule with an unexplained error is a rule people work around.
The same principle covers reviews and exceptions: an exception self-approved is
a control removed.

### Compliance that does not certify

§5.2.6 names seven frameworks. The obvious implementation returns a status per
framework, and it would be a lie — most of what those frameworks require happens
outside software. Training, physical security, vendor management, breach
notification: no code observes any of it.

So the catalogue has 18 controls, and every entry either **names a check that
reads a real fact** or **says why it cannot be checked here** (ADR-059). Twelve
are automated, six are `manual` with the reason, and a `manual` result is never
a pass. Live, a SOC 2 run reported:

> `controls 15: 9 pass, 2 fail, 4 manual, 0 n/a`
> `FAIL crypto.secrets_at_rest — Secrets are stored in the database in plaintext.`
> `FAIL supply_chain.security_scan — No security scan command is configured.`
> `MANUAL org.security_training — Happens outside this platform; nothing here observes it.`

The default install fails two controls **by design**, and a third once
governance exists but nothing is active. That is a more useful artifact than a
green badge, and it is the honest answer to a question the platform can answer.

### Risk, running the other way

§5.2.7 specifies *"lower = better, e.g. 18/100"* — the opposite direction from
Phase 6's trust score, about the same tools. So every response says
`lower_is_better: true` and **nothing converts between the two** (ADR-062). Seven
components matching the LLD's seven inputs, weights summing to 100, and the same
abstention rule: an unmeasurable component leaves both sides of the fraction,
and with nothing measurable the score is null rather than "no risk at all".

Writing the tests found a caveat worth stating: **two scores are only comparable
at similar coverage.** A score is risk out of what was measured, so a component
becoming measurable changes the denominator — and a newly-measured low-risk
component can *lower* the number even though more is now known. The test that
found it asserts the component rather than the aggregate, and
`describe()['comparability']` says so.

### A workflow whose stages are evidenced

Six stages, and the rule that makes them worth having: **every outcome is
sourced from something that actually happened.** Upload reads the registry
versions; validation reads the generation validation report; the scan stage
reads that report's scan checks; compliance reads a real run. A workflow whose
stages are ticked by whoever opened it approves whatever it is told to.

Live, on a tool with no version:

> `upload  blocked  source=registry_version  This tool has no version. There is nothing to review.`

and deciding it was refused: *"This review is blocked at upload… Fix that before
deciding."*

Auto-approval requires a score **below** the threshold. A null score never
auto-approves, because "nothing could be measured" is not "low risk" — and
treating it as such would auto-approve exactly the tools nobody has looked at.

### Exceptions that cannot outlive their reason

`expires_at` is not nullable, there is no "never", and the service caps it at 90
days: a column that can hold 2099 satisfies "all exceptions expire" on paper and
nothing in practice. Approval requires a risk assessment first — live, approving
without one was refused with *"The LLD's flow puts assessment before decision,
and a decision that skipped it cannot be reviewed later."* Revalidation falls due
at half the term, because a 90-day exemption reviewed on day 89 was not
reviewed. And expiry is checked at decision time as well as by the sweep, so an
exception past its date is not honoured just because nobody has swept.

### Four fail-safes, one stronger than asked

| failure                 | behaviour                                          |
|-------------------------|----------------------------------------------------|
| compliance scan timeout | `timed_out` is its own state; the review blocks    |
| risk calc failure       | keep the previous score, flag it stale with why    |
| audit write failure     | the transaction fails, so the action does not      |
| policy deploy failure   | roll the pointer back to the last active version   |

The audit one is worth reading twice. The LLD asks for *"queue durably; retry
before completing sensitive ops"*. Sutr writes the audit row **in the same
transaction as the action it describes**, so a failed write rolls the action back
with it — stronger than a queue, which still has a window in which the act
completed and the record is in flight (ADR-060). Claiming compliance with a
queue that does not exist would have been easier and worse.

### Six of seven governance points

§5.2 claims seven; the matrix recorded 1. `engine.POINTS` now names each point
and the module that gates it — upload, validation, generation, deployment,
publication, invocation — and says plainly that **monitoring is not gated**:
nothing evaluates governance continuously against a running tool (ADR-061). A
layer that claims seven and covers one is worse than one that covers six and
says which.

### Verification

Suite **2021 → 2100** green (79 new tests); ruff clean; migration 0040 applies,
rolls back and re-applies with **0 diffs**; contracts regenerated — 39 event
schemas, 93 `/v1` paths — and in sync.

**Live-verified against a running server** with two administrators:

- the author was refused approval of their own policy, with the message naming
  the fix; the second admin approved, published and activated it;
- an agent with `role: support` was then refused, and the decision named the
  **policy** and version the rule came from;
- rollback restored the previously active version;
- a SOC 2 run reported 9 pass / 2 fail / 4 manual, with the two failures being
  the default install's real gaps and the manual controls named;
- a simulated scan timeout came back `timed_out` with zero passes;
- a fresh tool scored **92/100 risk at 0.25 coverage**, with the explanation
  naming the five inputs that were not measured;
- a review blocked at `upload` with its evidence source, and deciding it was
  refused;
- an exception was refused approval until a risk assessment existed, then
  approved with the assessment stored; a 200-day request was refused at the
  boundary;
- `capabilities` reported **6 of 7** governance points gated with `monitoring`
  named, all four fail-safes with their modules, `certifies: false` and
  `lower_is_better: true`.

### Known limitations, stated rather than implied

- **Monitoring is not governed.** Nothing evaluates policy on a schedule against
  a running tool, and the capability endpoint says so.
- **Compliance does not certify.** Six of eighteen controls are `manual`; the
  engine answers "of what I can observe, what holds" and nothing more.
- **No policy simulation.** There is no way to ask what a *draft* would change
  before activating it — `/evaluate` reads the active set.
- **Policy evaluation is uncached**, so §5.2.5's <50 ms target remains NOT
  TESTED and unaddressed.
- **Exception sweeps and revalidation have no scheduler.** `expire_due` and
  `due_for_revalidation` exist and are tested; nothing calls them on a timer.
- **The risk engine's incident input is deployment failures**, which is the
  closest fact this platform holds. There is no incident record.
- **A review does not publish.** Reaching the publication stage does not move
  the registry tool; a person still runs the registry transition.
- **No frontend.** Policies, runs, reviews and exceptions have no console page.

## Phase 27 - ESDS Phase 10: billing, metering and monetization (2026-08-31)

LLD §5.1 asks for immutable usage events, eight pricing models, an immutable
ledger, invoices that never block metering, settlement with a configurable
revenue share, and dunning — under one sentence that decides most of the design:
*"prices evaluated at billing time, never during execution."*

`server/src/sutr/billing/` — pricing, ledger, invoices, settlement, dunning —
five tables in migration 0041, seven new columns on `usage_event`, and the four
routes §5.1.12 names at the paths it names them.

### Metering records facts; billing decides money

The shortcut is to price at execution: the tool call already knows the tenant,
the tool and the plan. `record_usage` instead takes **no price parameter and
writes none**, and `services/metering.py` does not import `sutr.billing` at all
(ADR-063). Three things follow, and all three are the reason:

- repricing is not retroactive, because yesterday's usage bills at yesterday's
  published plan version rather than at whatever number was frozen onto the row;
- a pricing bug cannot break a tool call it never touches;
- regenerating a period produces the same invoice, which is what makes a
  disputed invoice answerable.

The constraint is enforced structurally rather than by review. One test inspects
`record_usage`'s signature for `amount`, `amount_micros`, `price`,
`price_micros` or `plan_id`; another reads the module source for a billing
import. Both fail the moment somebody takes the shortcut.

### Money is integers; tiering is graduated

All amounts are integer micro-units and all rates basis points (ADR-064). No
float ever holds an amount — a rounding error in a ledger is not a rounding
error, it is a discrepancy somebody reconciles by hand.

All eight LLD models ship, dispatched from an `EVALUATORS` table whose keys a
test asserts equal the advertised model list, so a ninth model cannot be
advertised without an implementation behind it. Tiering is **graduated**: each
band is charged at its own rate, not the whole quantity at the band the total
lands in, which would make one extra call raise the entire bill.

Every line stores its arithmetic. Live, 250 calls on a two-band tiered plan with
a 5% discount and 18% GST came back as five readable steps:

```
usage: 250 unit(s) of tool_call
plan: standard v1 (tiered) → 80 USD
discount: 5% → −4 USD
GST: 18% → +13.68 USD
charge: 89.68 USD
```

An invoice a tenant cannot check is an invoice a tenant has to trust.

### The ledger only grows

`billing/ledger.py` has an `append` and no update or delete path — a test reads
the source and asserts neither exists. Undoing an entry writes a **compensating
entry**: opposite direction, same amount, pointing at the original, and a reason
is required. Both rows stay, because the mistake and the correction are both
part of the record (ADR-065). Voiding an invoice reverses every entry it
produced and keeps the invoice, in state `void`, with its reason.

Amounts are never negative; `direction` carries the sign. A negative amount is
refused rather than quietly meaning a credit.

### Usage nobody priced is reported, not billed at zero

An invoice over usage with no published plan could bill it at zero or drop it.
Both are decisions the platform is not entitled to make on a tenant's behalf, so
the invoice carries an `unpriced` array naming the kind, the quantity and why.

### The revenue share is configuration, recorded per run

`REVENUE_SHARE_PROVIDER_BPS` (default 8000 = 80% to the provider) is read on
**every** call rather than captured at import, may be overridden per run, and is
then written onto the settlement row — so changing the default later does not
alter what an old settlement says it paid (ADR-066). A test asserts the module
source contains no literal split, per build prompt §44. Settlement covers issued
invoices only: a draft is a calculation, not an obligation.

### What this does not do, said in the payload

Invoices are computed and settlements are calculated. **Nothing here collects or
pays out money** — that needs a payment processor whose API is not in the LLD,
and guessing one would break the no-hallucination rule. Rather than a caveat in
a README, it is a field: `collected_by_this_platform: false` on every invoice,
`paid_out_by_this_platform: false` on every settlement, `scheduler: null` on
dunning, and all of it in one place at `GET /v1/metering/capabilities`
(ADR-067). `record_payment` records an attempt made elsewhere; `mark_paid_out`
records a payout made elsewhere. Neither calls anything.

Dunning retries on a configured interval up to a configured maximum and then
**stops** — `next_attempt_at` goes null and `exhausted` is true — rather than
retrying forever.

### Two pricing tables, deliberately

Phase 6's `registry_pricing` is what a listing advertises; Phase 10's
`pricing_plan` is what an invoice is computed from (ADR-068). Merging them would
mean either that editing marketing copy changes what tenants are billed, or that
a published plan cannot be described in prose.

### Verification

Suite **2100 → 2181** green (81 new tests); ruff clean; migration 0041 applies,
rolls back and re-applies with **0 diffs**; contracts regenerated — 39 event
schemas, 107 `/v1` paths — and in sync.

**Live-verified against a running server** on a scratch database with two
tenants:

- 250 metered calls priced through a graduated tiered plan to 89.68 USD, with
  the arithmetic above returned on the line;
- a replayed `invocation_id` came back `deduplicated: true` with the original
  event's id, and no second row;
- a simulated generation failure recorded `state: failed, attempts: 1`, and the
  retry issued **the same invoice row** at `attempts: 2`;
- four dunning attempts: the fourth returned `next_attempt_at: null` and
  `dunning_exhausted: true`, and a fifth attempt was recorded as a fact without
  rescheduling — which is now its own test;
- voiding the issued invoice took the receivable from 89 680 000 micros to
  **0**, leaving four ledger entries of which two are reversals;
- a settlement failed to `paused`, retried on the same row to `complete`, and
  reported an 80% share it had recorded rather than looked up;
- the second tenant got **404** on the first tenant's invoice, void, payments,
  settlement retry and payout, empty lists for invoices, plans and settlements,
  a zero ledger, and a refusal naming the reason when it tried to run the first
  tenant's settlement.

### Known limitations, stated rather than implied

- **No payment is collected and no payout is made.** Attempts and payouts are
  recorded; the processor integration is BLOCKED — external API documentation
  required.
- **Nothing schedules anything.** `invoices.retryable()`, `dunning.due()` and
  the settlement retry all exist and are tested; no worker calls them on a
  timer, consistent with ADR-030's refusal to show a queue that does not run.
- **Token and data-transfer quotas are still unenforced.** Phase 10 added the
  fields; measuring them per call and checking a limit is a different job.
- **The platform's own Stripe subscription is unreconciled** with the LLD's
  TRIAL → ACTIVE → SUSPENDED → EXPIRED → RENEWED vocabulary.
- **Currency is per plan and not converted.** An invoice mixing plans in two
  currencies would sum them; nothing today creates one, and no FX rate source
  is wired.
- **No frontend.** Plans, invoices, the ledger and settlements have no console
  page.

## Phase 28 - ESDS Phase 11: observability (2026-08-31)

LLD §5.3 is six lines of prose and one figure. It asks for eight components in
a dedicated namespace, mTLS between services and collectors, RBAC dashboards,
tenant-isolated telemetry, PII masking, traces that span *Gateway → Discovery →
Runtime → Provider*, and a fixed field set on every log line.

The field set arrived in Phase 1 and the metrics surface predates the ESDS work
entirely. What was missing was everything that makes those two into
observability: a trace that survives a network hop, telemetry a tenant can
actually read, numbers for the asynchronous path, and somewhere to send it all.

### A trace is only a trace if it is continued

`observability/propagation.py` extracts W3C trace context from the inbound
request and injects it into every outbound provider call — HTTP and MCP alike.
The gateway middleware opens `sutr.gateway` as a child of the caller's span
when there is one, and sampling is `ParentBased`, so a request that arrived
sampled stays sampled here regardless of the local ratio. A cross-service trace
with holes in it is worth less than no trace at all (ADR-069).

Two things propagate independently, and the split matters: the **correlation id
always goes**, with or without the OpenTelemetry SDK, because it is ours and
costs nothing; the **trace context goes only when a span is recording**, because
advertising a `traceparent` for a trace nobody keeps invites a provider to
parent its spans onto an id that leads nowhere.

Four stages now carry `sutr.stage` — gateway, discovery, runtime, provider —
and each is a real span with a real parent.

### What a span may carry

Attributes describe shape, never content (ADR-070), and the tests are the
enforcement:

- tool arguments and results never appear;
- the provider span carries the **host and method, never the URL**, because
  credentials can ride in a query string (ADR-009);
- the discovery span carries the **length** of the intent, not the intent — a
  search query is a user's sentence and can contain anything;
- every span carries the correlation id, which is the join between a trace and
  the log lines written underneath it.

Log lines gained `trace_id` and `span_id` when a trace is recording, and
`region` now comes from configuration rather than waiting for a call site to
remember it. `LogEntry` gained `correlation_id` and `trace_id` (migration 0042),
filled from the ambient context by a default factory rather than by five call
sites that would each have to remember.

### Telemetry a tenant can read, from tables that have owners

The shared stack holds every organisation's data, so none of it is safe to hand
to a tenant, and `/metrics` still carries no tenant label anywhere — now
enforced by a test that walks every exported series and fails on a label named
for an org, tenant, user or key.

`/v1/observability` instead serves a tenant its own call rate, error ratio,
latency percentiles, per-provider breakdown and the timeline of one operation,
from this platform's own tables, scoped by the query itself (ADR-071). An
unknown correlation id and another tenant's correlation id are the same 404,
because any other answer would turn the route into a way to ask whether a
competitor made a particular call.

### Numbers for the asynchronous path

`sutr_provider_requests_total` and `sutr_provider_request_duration_seconds`
close the "provider latency" gap; `sutr_queue_depth` and
`sutr_event_relay_lag_seconds` close "queue depth" and "lag". The gauges are
sampled **on the scrape**, so the number a scrape returns was true when it was
returned — and a sample that cannot reach the database counts itself and leaves
the gauges alone, so they read as stale rather than as zero (ADR-072). Zero is a
number an alert acts on.

The LLD's list says "Kafka lag". This install's metric is named after the
outbox, because on an install with no broker the outbox *is* the queue.

### The stack, and what makes it more than a directory of YAML

`deploy/observability/` ships otel-collector, Prometheus, Alertmanager, Loki,
Tempo, Grafana and node-exporter as a pinned compose stack, with eleven alert
rules, a nine-panel dashboard, a Loki→Tempo link on the `trace_id` field, and a
runbook section per alert.

Every configuration file was validated against the real binary at its pinned
version — `promtool check config`, `amtool check-config`, `otelcol validate`,
`loki -verify-config`, `tempo -config.verify` — and the whole stack was started
and queried. And on every commit, the suite asserts that every metric an alert
or a panel names is a metric this build exports, that every alert points at a
runbook section that exists, and that every image is pinned. Renaming a metric
fails the build; that was checked by renaming one (ADR-073).

### Verified live

Against the running stack, with a real provider hop:

- one tool call produced **one trace with three spans** in Tempo — `sutr.gateway`
  (server) → `sutr.tool_call` (runtime) → `sutr.provider.request` (client) — all
  three carrying the correlation id, with `service.name` and `cloud.region` on
  the resource;
- a request carrying `traceparent: 00-4bf92f…-00f067aa0ba902b7-01` produced a
  gateway span **parented to `00f067aa0ba902b7` inside the caller's trace**;
- the provider echoed back the headers it received: `Traceparent` and
  `X-Correlation-Id`;
- the JSON log line for the call carried tenant, provider, tool, correlation id
  and the same trace id;
- `/v1/observability` returned that tenant's two calls, its p50/p95, its
  per-provider breakdown and the operation timeline — and a second tenant got
  zero calls, an empty operations list and a 404 on the first tenant's
  correlation id;
- Prometheus loaded all eleven rules, Alertmanager accepted its configuration,
  and Grafana provisioned three datasources and the dashboard.

### Known limitations, stated rather than implied

- **The stack is single-replica.** §5.3 asks for HA — replicas, federation,
  clustering — and this is one container each with local storage. Phase 12.
- **kube-state-metrics is absent.** It reports on a Kubernetes cluster; this is
  a compose file.
- **mTLS is half-wired.** The three OTLP TLS settings reach the exporter and
  are tested; the collector's receiver TLS block is written but commented out,
  and no certificates are shipped — generating a CA for someone else's
  deployment is not a config file's job.
- **Fluent Bit is NOT TESTED.** Its configuration is written and commented out
  of the compose file: it needs the host's Docker log directory.
- **RBAC dashboards are partial.** Grafana anonymous access is off; no roles
  are provisioned.
- **No broker-level consumer lag.** When the Kafka bus is enabled, its lag is
  not exported — only the outbox's.
- **Telemetry is not encrypted at rest** in any of the three backends.
- **`runtime_id` is still carried by nothing.** There is no runtime registry to
  populate it from.
- **No frontend.** The tenant telemetry surface has no console page.

## Phase 29 - ESDS Phase 12: running more than one of everything (2026-09-01)

LLD §5.4 asks for HA PostgreSQL, Kafka, Redis, OpenSearch, Qdrant, Neo4j, MinIO
and Vault; §5.7 for regions that upgrade independently; §5.8 for self-healing
and scaling per layer.

Six of those eight stores have no client anywhere in this codebase. Shipping a
Neo4j StatefulSet nothing connects to would satisfy a checklist and tell every
reader of the repository something false — that the knowledge graph runs on
Neo4j rather than in two Postgres tables. So this phase ships infrastructure for
what the build actually uses, and a table naming each store the LLD asks for and
whether there is a client for it (ADR-077).

The rest of the phase is the part that is this repository's to get right: what
the *software* has to do differently once there is more than one of it.

### The bug that horizontal scaling would have introduced

`sweep_deployments` meters runtime minutes. Two replicas sweeping the same five
minutes bill a tenant for ten — nothing raises, nothing logs, the number is
just wrong. Before this phase the only defence was a comment telling operators
to set `EVENT_RELAY_ENABLED=false` on all but one replica.

Now four jobs — maintenance, the deployment sweep, source sync and the event
relay — run under a lease (`leader_lease`, migration 0043). A replica takes the
row, renews it every 15 seconds, and stops renewing when it dies; another takes
over after 45 seconds (ADR-074). A lease rather than an advisory lock for three
reasons: it survives PgBouncer in transaction mode, where a session-level
advisory lock is worse than useless; it behaves the same on SQLite, so it is
tested in the ordinary suite; and `SELECT * FROM leader_lease` answers "which
replica is sweeping?" without a debugger.

### A standby that knows what it is

A follower region's database physically refuses writes, so the application
there turns every write into a 500 saying *"cannot execute INSERT in a
read-only transaction"* — an error an agent cannot act on and will retry against
the same instance. `PLATFORM_MODE=standby` serves reads, runs no leased jobs
(all of them write), and refuses writes with a 503 naming the mode, the region
that owns writes, and a `Retry-After` (ADR-075).

Two details earned their tests: anything unrecognised in `PLATFORM_MODE` means
*active*, because a typo must not take a region out of service; and POSTs that
only read — discovery search, the policy decision point — are allow-listed by
exact path, with one test asserting each path is a real route and another
asserting those handlers contain no `session.commit()`.

### Probes that answer the question they were asked

`/health` returned `{"status": "ok"}` unconditionally. It still does — the
compose healthcheck and `fly.toml` point at it — and it is now an alias for
`/health/live`, which checks the process and deliberately nothing else.
`/health/ready` checks the database and reports the instance id, region, mode
and the leases this replica holds. A liveness probe that failed during a
database outage would restart every replica at once (ADR-076).

### What is still per-process, written down

Rate-limit windows, the discovery cache, approval long-poll waiters and the
Prometheus registry are per replica by design. `GET /v1/platform/scaling` lists
each one with what changes when there is more than one and what to do about it,
and a test asserts every entry still names a module attribute that exists.

### Verified live, on a stack that was actually running

- **Replication**: async streaming, replay lag under 2 ms; a row written on the
  primary was readable on the replica; a write *to* the replica was refused as
  read-only — the exact error standby mode exists to keep away from agents.
- **Migrations through the pooler**: `alembic upgrade head` reached 0043
  through PgBouncer in transaction mode before any replica served.
- **Load balancing**: alternating requests through Caddy answered by two
  different instance ids.
- **Leadership**: one replica held all four leases and the other held none. A
  graceful stop handed them over; a **SIGKILL** was followed by takeover 61
  seconds later — a 45-second lease plus a 15-second poll — generation 2 → 3.
- **Standby**: a third instance against the read-only replica reported ready,
  served GETs, served the allow-listed `POST /v1/discovery/search`, and refused
  `POST /v1/registry/tools` with a 503 naming the primary region.
- **Backup and restore**: a real dump restored into a fresh database — 73
  tables, schema 0043, checksum verified. Restoring over a non-empty database
  was refused without `FORCE=1`; a dump edited after its manifest was written
  was refused outright.
- **The chart**: `helm lint`, `helm template`, and all nine rendered manifests
  validated strictly against Kubernetes 1.31 schemas with `kubeconform`. It
  refuses to render without an image, with a `latest` tag, or without a Secret.

### Two things found by running it

**PgBouncer rejects `DB_STATEMENT_TIMEOUT_MS`.** It travels as a libpq startup
parameter, and PgBouncer refuses startup parameters it does not know: every
replica died at boot with *"unsupported startup parameter in options:
statement_timeout"*. Telling PgBouncer to ignore it would be worse — the
setting would look configured and do nothing — so behind a pooler the timeout
goes on the role, which the stack's init script now does, with the caveat
written into `db.py` where the setting is built.

**A supervisor that cancelled its work could not itself be cancelled.**
Awaiting a cancelled task re-raises its `CancelledError` in the awaiting
coroutine; swallowing that left the supervisor's own cancellation state
confused, so it stopped renewing its lease, logged nothing, and would have hung
the process on shutdown — a leader that had quietly stopped leading while
looking perfectly healthy. `asyncio.wait` observes the outcome without adopting
it. There is now a regression test whose only assertion is that shutdown
returns at all, and leadership changes are logged on every transition rather
than only when a task happened to be running.

### Known limitations, stated rather than implied

- **No availability number has been measured.** §5.4 asks for 99.99% on
  PostgreSQL and §5.8 for 99.95% platform availability. NOT TESTED.
- **No automatic failover.** The replica is a warm copy; promotion is
  `pg_ctl promote` plus repointing the pooler. The LLD's connection path names
  a proxy for exactly this and there is none here.
- **One host.** Every replica in the verified stack shares a kernel, a disk and
  a power supply. What was shown is that the software is correct with more than
  one of it, not that the deployment is redundant.
- **The Helm chart has never been applied to a cluster.** Rendered and
  schema-checked only, and its own `NOTES.txt` says so.
- **Nothing routes reads to the replica.** It exists, it streams, and the
  application does not read from it.
- **Backups are not encrypted**, and the manifest says `"encrypted": false`.
- **Kafka, Redis, OpenSearch, Qdrant, Neo4j and MinIO are still absent**, and
  the six §5.4 rows for them still read NOT IMPLEMENTED.

## Phase 30 - ESDS Phase 13: DevSecOps, resilience, load and chaos (2026-09-01)

Six security workflows had been written in Phase 1 and never executed. The
traceability matrix recorded them as NOT TESTED, which was accurate and
comfortable. Running them was most of this phase, and it was not a formality.

### What the gates found when they were finally run

**The dependency audit would have failed on every run**, for a reason unrelated
to security: `pip-audit --strict` against the installed environment trips over
this repository's own package, which is not on PyPI. It now audits the locked
set (`uv export --no-emit-project`), which is both a working gate and a better
one — it audits what will be installed rather than what happens to be.

It then found **30+ advisories across 9 packages**. Two mattered:

- **starlette 1.0.0** — five advisories, including a **path-based
  authorization bypass**: `request.url` was rebuilt from an unvalidated `Host`
  header, so middleware gating on `request.url.path` could be fooled. This
  platform makes authorization decisions in middleware. The standby middleware
  added in Phase 12 happens to read `scope["path"]`, the form that was never
  affected — luck rather than knowledge, until now.
- **python-multipart 0.0.24** — five, including quadratic parsing of
  `;`-separated bodies and a separator differential that lets form fields be
  smuggled past an inspecting proxy.

`pip-audit --strict` now reports *"No known vulnerabilities found"*. `mcp` is
held at `>=1.28.1,<2`: 1.28.1 clears its three advisories, and 2.x renames the
transport entry points — a migration, not something to smuggle into a security
bump.

**Secret detection would have failed on day one** with twelve false positives,
which is how a gate comes to be disabled in week one. `.gitleaks.toml`
allowlists exactly four things, each named and reasoned.

**SAST found 84 findings; there are now zero**, and 62 of them were fixed
rather than suppressed:

- 54 unpinned GitHub Actions, now pinned to commit SHAs — and while doing it,
  `aquasecurity/trivy-action@0.28.0` turned out to be **a reference that does
  not exist** (the tag is `v0.28.0`), so that step would have failed to
  resolve.
- A **root-running production image**, which contradicted the Helm chart's own
  `runAsNonRoot: true` with uid 1000 shipped in Phase 12. The image now runs as
  uid 1000; verified by running it, migrating and serving.
- Five `len(session.exec(select(...)).all())` counts, which load every row to
  take its length. Now `SELECT count(*)`.
- The CLI's browser-open used `shell: true` on Windows, putting a
  server-supplied URL on a command line where `&` starts a second command.

Every acceptance is recorded twice — at the site and in
`docs/SECURITY_GATES.md` — so neither can drift without the other becoming
obviously wrong. A licence check, which the matrix had explicitly listed as
missing, now exists.

### The five failure-handling patterns

`server/src/sutr/resilience/` — breaker, retry, timeouts, domains.

**The circuit breaker** (ADR-078) is keyed by host, because two integrations
pointing at the same upstream share its fate. Two details carry the value:
half_open lets through **exactly one** call — a provider that has just come
back must not be load-tested by the thing protecting it — and only failures
that describe the provider count. A 404 is a provider working correctly and
disagreeing; quarantining on a run of them would take a healthy provider from
every caller. A 5xx counts. An unsafe-URL refusal does not: that call never
left the process.

A refused call **raises rather than returning an error result**, because it
never happened — returning one would meter it and log it as `executed`.

**Retries** (ADR-079) are narrower than most: only transport failures, only on
safe methods. A response that arrived reached the provider's application, and
this platform does not know whether that tool was idempotent. Backoff is
exponential with full jitter, because without jitter everything that failed
together retries together.

**Timeouts** are separated into connect, read, write and pool, each bounded by
the overall budget. **Failure domains and severities** are declared in code
with, for each domain, the sentence that actually matters: what keeps working.

### Measured, not asserted

A live chaos run against a provider whose host refuses connections:

```
attempt 1: 10.138s   attempt 4: 10.081s
attempt 2: 10.040s   attempt 5: 10.124s
attempt 3: 10.178s   attempt 6:  0.009s   ← the circuit is open
```

**10.1 seconds to 9 milliseconds**, and `/v1/resilience/providers` lists the
host as quarantined. The 10 seconds rather than 5 is the retry doing its job: a
failing GET costs two connect timeouts.

The load baseline, recorded in `qa/load/RESULTS.md`: **484 req/s across four
read endpoints, 0 failures, p95 124 ms** — on one uvicorn worker against
SQLite, with the load generator sharing the host. That does not approach the
LLD's 100 000 invocations/minute and is not offered as evidence about it; the
performance rows stay NOT TESTED. It is a repeatable number so that a change
making the gateway several times slower is noticed by somebody other than a
customer.

Of the seven chaos scenarios §5.8 names, **three have been run** (provider
timeout, pod kill, broker outage) and one partially (database failover). The
other three need a fault-injection proxy, a cluster and a second region, and
`qa/chaos/README.md` says "not run" rather than shipping a script that implies
otherwise.

### Two bugs found by writing the tests

A supervisor that had cancelled its work could not itself be cancelled —
awaiting a cancelled task re-raises its `CancelledError`, and swallowing it
left the supervisor's cancellation state confused. It stopped renewing its
lease, logged nothing, and would have hung shutdown. (Carried over from Phase
12, fixed here in the same sweep.)

And `str(httpx.ConnectTimeout(...))` is empty, so an operator whose provider
had failed five times read `last_error: null`. The class name is a poor message
and a much better nothing.

### Known limitations, stated rather than implied

- **The workflows have never run in GitHub Actions.** Every gate was executed
  locally, with Docker. They remain NOT TESTED in CI.
- **Cosign signing has never run**, because no release has been cut.
- **Bulkheads are not implemented** — every request shares one process and one
  database pool. `GET /v1/resilience/policy` says so in a field.
- **Provider health does not feed the trust score**, and nothing alerts ops per
  provider.
- **Breaker state is per replica.** N replicas discover a dead provider up to N
  times; declared in `platform/scaling.py`.
- **The load figure is one worker on SQLite** sharing a host with the load
  generator, and establishes nothing about the LLD's targets.
- **Three chaos scenarios have never been executed.**
- **No E2E browser tests.** The LLD names Playwright; there are none.
- **DORA metrics are not tracked** (§5.7.6 remains NOT IMPLEMENTED): they need
  deployment history this repository does not have.

## Phase 31 - the console for the /v1 platform (2026-09-01)

Thirteen phases built 117 `/v1` endpoints and the console called **none of
them**. Every phase entry above ends its limitations with "No frontend", which
was honest and still added up to a platform whose whole second half could only
be exercised with `curl`.

Twelve pages now cover it: Registry, Discovery, Sources, Documentation,
Runtimes, Governance, Agents, Revenue, Telemetry, Platform, Resilience, Events.

### What the pages are for

Not dashboards. Each one shows the thing its service is *actually* uncertain
about, because that is what a console is for:

- **Discovery** shows the ranked results and, underneath them, which stage
  spent the time and which rankers were unavailable — "nothing matched" and
  "matched, ranked by keyword because the vector index was down" look identical
  until something says so.
- **Registry** shows trust by component, not as one number. "62" answers
  nothing; "security scans: unavailable, no runtime artifact has been built"
  answers what to do next. A component with no evidence reads *unavailable*,
  never zero.
- **Revenue** shows each invoice line with the plan version it was priced by
  and the arithmetic that produced it, and says on every invoice that this
  platform did not collect the money.
- **Platform** answers the question that otherwise needs a shell: which replica
  is holding each lease.
- **Resilience** shows the circuit per provider and says the scope out loud —
  this replica only.

### The three bugs that only a browser could find

The pages compiled, linted and built long before they worked.

**`npx tsc --noEmit` was checking nothing.** The root `tsconfig.json` has
`"files": []` with project references, so the bare command type-checks an empty
program and exits 0. Every "typecheck passed" before this was meaningless; the
real command is `tsc -p tsconfig.app.json --noEmit`, and running it surfaced 40
real errors at once.

**Four endpoints answer with a bare array**, not an object — `/v1/sources`,
the four documentation collections, and both event routes — while others name
the collection alongside their own explanatory fields (`/v1/billing/plans`
returns `plans` beside `models`, `tiering`, `money`). A page that assumes the
wrong one renders an empty table and reports nothing, which is the worst
failure available: indistinguishable from "you have none". The client now
extracts the collection and *throws* on a shape that is neither.

**The tabs were not clickable.** Rendering the page and reading its heading was
not enough — Playwright's click failed with "`sutr-page__body` intercepts
pointer events", because every existing page puts `SutrTabs` inside
`SutrPageHeader` and mine put them in the body. Five pages had tabs that looked
correct and could not be used.

Field names came from the running server rather than from the LLD: an agent's
`kind` (not `agent_type`), a tool's `lifecycle_state` (not `state`), a pass's
`resource` and `tools` (not `tool_id`), a ledger's `receivable`/`payable`
balances, an invoice line's nested `plan` object.

### Verified

Against a live backend with seeded data, driving Chrome through Playwright:
all twelve routes render with **no console errors and no page errors**; the
eight tabbed views open and show their content; and the pages display real
rows — the registry shows its tool, agents its identity, events its
`tool.registered`, platform its four held leases, resilience its eight failure
domains. All 40 endpoints the console reads return 200. `tsc -p
tsconfig.app.json`, ESLint and `vite build` are clean, and each page is its own
lazy chunk.

`vite.config.ts` gained a `/v1` proxy — without it every new page received
Vite's `index.html` instead of JSON — and `SUTR_API_TARGET`, because the proxy
otherwise points at whatever is on port 4747, which on a machine also running
the container is a different database and a confusing set of 401s.

### Known limitations, stated rather than implied

- **These pages read and act; they do not create.** There is no form for a new
  policy, source, document upload or agent — the console can approve, revoke,
  publish, issue, retry and close, but a new object is still created through
  the API or another page.
- **Some panels render JSON.** Lifecycle, provisioning rules, extracted
  documentation and capability reports are shown as formatted JSON rather than
  bespoke views. That is a deliberate stop: a shape displayed as prose that
  later changes silently lies, and JSON does not.
- **No frontend tests.** The verification above was a browser session, not a
  suite; there is no Playwright or Vitest coverage holding these pages.
- **Untested paths remain.** Governance compliance runs, source drift apply and
  dismiss, and event retry were rendered but never exercised against real
  objects — there were none to exercise them with.
- **No pagination.** Every list asks for one page of up to 200 rows.

## Final state
Suites: **server 953**, Python SDK **42**, TypeScript SDK **36**; ruff clean; UI `tsc`+`vite` and CLI `tsc`+`tsup` green; 29 migrations applied cleanly on a populated dev database.
Per-row implementation status for all 51 functionalities, with file:line evidence for every claim, lives in `SUTR_BUILD_PROMPT.md` §3 and is re-audited rather than remembered.
Deferred by design, all documented above and in-code: live-account verification of the three cloud providers, a Kubernetes provider, Azure Container Apps log retrieval, SDK publishing (needs registry credentials), TOTP-secret encryption, SSRF DNS pinning, multi-worker approval event bus and shared rate-limit store, usage-based billing quotas, non-JSON request bodies, query/cookie `apiKey` and OAuth2 grants for compiled APIs.
Governance (unify REST/MCP pipeline, log redaction, audit trail, exact-args-forever, decision-row locking) · OpenAPI compiler (§17–27) · HTTP runtime · generated servers · deployment providers (K8s/Argo/Swaraj) · observability & metering · SDK/CLI · frontend completion · security hardening · E2E validation.
