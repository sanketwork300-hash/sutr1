# Sutr — Phase 0 Repository Audit (Sutr baseline)

Date: 2026-08-19. Read-only audit of the supplied Sutr repository; no code was modified.
Baseline verification: **328/328 server tests pass** (`uv run pytest`, ~2 min), CLI `tsc --noEmit` passes, UI `tsc -b` passes (one TS `baseUrl` deprecation warning with TS 6). CI is green-shaped (ruff + pytest + prettier).

---

## 1. What Sutr actually is

A universal tool gateway for AI agents:

```
MCP client (Claude Desktop/Code, Cursor, …)
        │ Streamable HTTP (stateful, session-id + SSE notifications)
┌───────▼────────────────────────────────────┐
│ FastAPI server :4747                       │
│  /mcp  → MCP gateway (11 sutr__ meta- │
│          tools; upstream tools addressed    │
│          by (integration_id, tool_name))    │
│  /api  → REST management API               │
│  OAuth 2.1 AS (DCR, PKCE, refresh, revoke) │
│  serves built UI from ui_dist/             │
└───────┬────────────────────────────────────┘
        │ httpx / MCP client (fresh connection per call)
   49 bundled integrations (45 remote_mcp + 4 custom REST)
   + per-org custom MCP servers + custom (manual) REST APIs
```

- **Stack**: FastAPI + SQLModel + Alembic (SQLite default / Postgres via `DATABASE_URL`), `mcp` Python SDK, React 19 + Vite + Zustand UI, TypeScript Commander CLI (`ap`), Stripe billing, Resend email, PostHog analytics, AWS KMS optional secrets encryption, Docker/Caddy/fly.io deploy.
- **Tenancy**: `User ⇄ OrgMembership ⇄ Org`, but effectively 1 user = 1 org. `role` ("owner"|"member") is read in exactly one place (`api/billing.py:47`); "member" is never assigned; there are no invites, no member management, no workspaces.
- **Governance**: deny-by-default per-tool policy (`allow | require_approval | deny`) in `ToolExecutionSetting`; human approvals with SHA-256 argument binding (`approvals/normalize.py`) for approve-once; `allow_tool_forever` drops argument binding by design; optional TOTP gate on decisions; long-poll wakeups via in-process asyncio pub/sub.
- **Secrets**: `SecretsBackend` abstraction with `db` (plaintext — the default) and `db_kms` (AWS KMS envelope AES-256-GCM). Secrets never serialized to the frontend.
- **MCP**: Sutr is both an MCP **server** (gateway) and a full **OAuth 2.1 authorization server** for MCP clients, and an MCP **client** toward upstream remote servers.

## 2. Subsystem inventory (what exists and works)

| Subsystem | Location | State |
|---|---|---|
| Identity: signup/login/logout-less JWT, email verify (link+code), password reset/change, TOTP 2FA (post-login gate only), Google SSO, login lockout | `api/user_auth.py`, `api/email_verification.py`, `api/password_*.py`, `totp.py`, `api/google_login.py` | Working, tested |
| Orgs | `models/org.py`, membership | Minimal: auto-created at signup, no lifecycle |
| API keys | `api/api_keys.py` | Working; org-wide, unscoped, no expiry |
| 49 bundled integrations | `integrations/bundled/` + `registry.py` + `types.py` | Working. 45 `remote_mcp`, 4 `custom` (gmail, google_calendar, slack, resend). 44 support OAuth (3 provider-configured, 41 via discovery+DCR), 31 support token |
| Custom remote MCP servers | `api/custom_mcp.py` | Working (none/token/oauth auth) — **no SSRF validation** |
| Custom REST APIs (manual builder) | `api/custom_api.py`, `api_client.py` | Working; validated, SSRF-checked, hardened test endpoint |
| MCP gateway | `mcp/server.py`, `asgi.py`, `management_tools.py`, `notifications.py` | Working; stateful Streamable HTTP; 11 meta-tools |
| MCP OAuth AS | `mcp/oauth_provider.py`, `api/oauth_server.py` | Working (DCR, PKCE, refresh rotation, revocation, consent UI) |
| Integration OAuth | `auth_start.py`, `mcp/oauth.py`, `api/auth.py` | Working (provider-env or discovery+DCR; PKCE always; refresh pre-flight + reactive) |
| Tool discovery/cache | `models/tool_cache.py`, `mcp/refresh.py` | Working; 24 h TTL + hourly loop; duplicated read paths (REST vs MCP) |
| Approvals | `approvals/*`, `api/tool_approvals.py` | Working, well-tested; arg-hash binding; TOTP; long-poll |
| Logs | `models/log.py`, `api/logs.py` | Working; per-tool-call record; **no redaction, no retention, no user attribution** |
| Billing | `billing/*`, `api/billing.py` | Working (Stripe checkout/portal/webhook); one quota (5 free integrations); `require_plus` dead code |
| Secrets | `secrets/*` | Working abstraction; plaintext default |
| Admin | `api/admin.py` | User search, impersonation (hardened), waitlist |
| UI | `ui/src` | Full auth flows, catalog/connect, tool modes, custom API wizard, playground, approval page, API keys, settings, admin, billing |
| CLI (`ap`) | `cli/src` | auth (API-key only), integrations list/add/remove, tools list/describe/call `--wait`, await-approval; human/json/toon output |
| Tests | `server/tests` | 328 pass; in-memory SQLite via `create_all` (migrations untested); SSRF module stubbed out in tests |
| Docs | `docs/` | teeny site; API reference (815 lines) + approvals doc exist but missing from sidebar |
| Deploy | Dockerfile (UI+server single image), compose (+Caddy prod), fly.io, install.sh | Working; fly deploy not gated on CI |
| **OpenAPI** | `server/src/sutr/openapi/__init__.py` | **0 bytes — completely absent** despite AGENTS.md claiming a generator |

## 3. Gap map vs. Sutr requirements

**Missing entirely (must be built):**
- OpenAPI import/validation/`$ref`-safe parsing/normalization/IR/MCP compilation (§17–§27) — nothing exists.
- Generated standalone MCP servers, generated tests, Docker packaging (§28–§31).
- Deployment abstraction, Kubernetes/Argo CD/Swaraj Cloud providers, deployment monitoring, runtime secret injection (§32–§38).
- Workspaces; RBAC (roles beyond a vestigial string, permissions, centralized `AuthorizationService`); org lifecycle (invites, members, roles, rename/delete) (§5).
- Usage-event metering ledger (§46) — analytics go to PostHog only; billing has no durable usage source.
- OpenTelemetry tracing, Prometheus metrics (§16, §60) — zero instrumentation.
- Control-plane audit log (§15/§47) — logins, key management, policy changes, impersonation, integration installs are not durably audited; tool-mode changes destroy prior state invisibly.
- SDKs (Python/TypeScript) (§44).
- UI pages: org/members, workspaces, MCP registry management, OpenAPI import wizard, deployments, usage, global logs, approvals inbox (§48–§49).
- Wildcard/category policies; "approve exact args forever" (`approved_exact` is a dead enum value).
- Rate limiting beyond 3 IP limiters on auth endpoints; nothing on tools/MCP; no proxy-IP awareness.

**Present but needing hardening/refactor (preserve behavior, fix seams):**
- REST vs MCP duplication: auth (`dependencies.py` vs `asgi.py`, inverted precedence), execution pipeline (`api/tools.py:330-622` vs `mcp/server.py:110-476`), cache reads — Sutr's "one canonical pipeline" (§13) requires extracting a shared service layer.
- SSRF: `upstream_safety.py` is solid but unenforced on custom MCP URLs and `auth_start.py` discovery; DNS-rebind TOCTOU open (resolved IPs never pinned); zero direct tests (stubbed in the suite).
- Secrets: plaintext default backend; `value_hash` (unsalted) + `prefix` (12 plaintext chars) stored even under KMS; no AAD binding; no rotation.
- Approvals: no RBAC on decisions; TOTP opt-in per user; check-then-act races (no row locking); no expiry sweeper; single-process event bus (breaks multi-worker); normalization lacks unicode/number canonicalization.
- Logging: no redaction of `args_json`/`result_json`; no `user_id` attribution; outcome vocabulary drift (`pending` vs `approval_required`) already causes an expiry-decoration bug; unindexed filter columns; no retention.
- Rate limiting collapses behind the shipped Caddy proxy (no `X-Forwarded-For` handling) — one attacker can exhaust global login buckets.
- No registration password-length check; 7-day JWTs with no logout/revocation; TOTP secret plaintext; revocation table unpruned and read per request.
- Billing: `tier` column not authoritative vs `_is_plus`; no webhook event idempotency; GET-with-side-effect subscription creation.
- DB: FK enforcement off on SQLite (no PRAGMA); `alembic/env.py` missing `Secret`/`Subscription` imports (autogenerate would emit DROPs); no cascades/soft-delete; mixed naive/aware datetimes; JSON-as-string columns.
- Datadog-style two-key providers broken (single `token_secret_id`, first `TokenAuth` wins). `EnvVarAuth` dead. Custom APIs can't use OAuth.
- Open review findings in `review-comments.md` (P2 auth-mode-switch validation bug, P3 description-clear bug confirmed still plausible; the P1 test-endpoint exfiltration appears addressed by the guard at `custom_api.py:405-418` — re-verify in Phase 13).

## 4. Reuse decisions (Sutr will keep)

1. **Whole approval subsystem** (`approvals/`, `tool_approvals` API, TOTP gate, arg-hash binding) — extend, don't rewrite.
2. **All 49 bundled integrations + `types.py`/`registry.py`** — preserve as native integrations; extend the type system rather than replace.
3. **MCP gateway + OAuth AS** (`mcp/`) — protocol behavior is correct and tested; refactor internals onto shared services only.
4. **Integration OAuth machinery** (`auth_start.py`, `mcp/oauth.py`) — add SSRF screening, keep flows.
5. **Secrets abstraction** — keep interface, fix defaults/leakage, add rotation.
6. **`ApiTool`/`Param` declarative HTTP mapping + `api_client.py` runtime** — this is the natural execution target for the OpenAPI compiler (compile OpenAPI → `ApiTool` IR-compatible records).
7. **UI + CLI as-is**, extended with new pages/commands; existing REST routes stay stable.
8. **Alembic migration chain** — continue from 0022; never rewrite history.
9. **Test harness** (conftest patterns) — extend; add migration-based tests.

## 5. Migration risks

- No git repository in this working copy — initialize version control before Phase 1 so every change is reviewable/revertable.
- `conftest._ENGINE_CONSUMER_MODULES` hand-list: any new module importing `engine` at module scope silently escapes test isolation — prefer lazy engine access in new code.
- Router include order in `main.py` is load-bearing (`custom*` before the `/{integration_id}` catch-all).
- `.env` overrides real env (`load_dotenv(override=True)`) — changing this alters container behavior; needs a deliberate, documented flip.
- Single-process assumptions (approval events, notifications, in-memory rate limits) must be respected until a shared-bus phase; adding workers today silently degrades approvals.
- The MCP legacy-JWT branch and `.first()` org resolution will break the moment multi-org lands — org context must become explicit (JWT `org_id` claim already has latent support in `dependencies.py:220-231`).
- Migrations only run via compose/fly; local dev against uvicorn directly needs `alembic upgrade head` first.

## 6. Detailed reports

Full per-subsystem audit reports (server core/auth, data layer, MCP+integrations, approvals/secrets/billing, UI+CLI, tests/docs/devops) are preserved in the session workspace and summarized above. Key file references are inline throughout.
