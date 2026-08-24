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

## Final state
Suites: **server 953**, Python SDK **42**, TypeScript SDK **36**; ruff clean; UI `tsc`+`vite` and CLI `tsc`+`tsup` green; 29 migrations applied cleanly on a populated dev database.
Per-row implementation status for all 51 functionalities, with file:line evidence for every claim, lives in `SUTR_BUILD_PROMPT.md` §3 and is re-audited rather than remembered.
Deferred by design, all documented above and in-code: live-account verification of the three cloud providers, a Kubernetes provider, Azure Container Apps log retrieval, SDK publishing (needs registry credentials), TOTP-secret encryption, SSRF DNS pinning, multi-worker approval event bus and shared rate-limit store, usage-based billing quotas, non-JSON request bodies, query/cookie `apiKey` and OAuth2 grants for compiled APIs.
Governance (unify REST/MCP pipeline, log redaction, audit trail, exact-args-forever, decision-row locking) · OpenAPI compiler (§17–27) · HTTP runtime · generated servers · deployment providers (K8s/Argo/Swaraj) · observability & metering · SDK/CLI · frontend completion · security hardening · E2E validation.
