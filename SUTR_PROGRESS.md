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

## Phases 5–14 — pending
Governance (unify REST/MCP pipeline, log redaction, audit trail, exact-args-forever, decision-row locking) · OpenAPI compiler (§17–27) · HTTP runtime · generated servers · deployment providers (K8s/Argo/Swaraj) · observability & metering · SDK/CLI · frontend completion · security hardening · E2E validation.
