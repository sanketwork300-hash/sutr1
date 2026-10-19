# Sutr — Security Audit Findings

Date: 2026-10-17
Scope: `server/src/sutr/**` (FastAPI backend, MCP gateway, OAuth, billing, governance, secrets), `ui/src/**`, `cli/src/**`, deploy config.
Method: full read of auth/authz/pipeline code plus targeted review of every API router, the MCP transport, the outbound proxy, the secrets layer, and the clients. Every finding below was confirmed by tracing the actual code path; nothing here is based on pattern-matching alone.

Severity scale: **Critical** = cross-tenant compromise or full account/identity takeover with low preconditions. **High** = privilege escalation, credential exposure, or policy bypass inside a tenant. **Medium** = meaningful weakening of a control that needs an extra precondition.

Companion file: `LOGIC_BUGS.md` covers logic/correctness defects that are not primarily security issues.

---

## Summary table

| # | Severity | Area | Title |
|---|----------|------|-------|
| S1 | Critical | MCP transport | StreamableHTTP sessions run as the identity that created them; session id = cross-tenant access and revocation bypass |
| S2 | Critical | AuthZ | API keys bypass the entire role matrix; a `developer` escalates to `owner` on every `/v1` surface |
| S3 | Critical | Org invites | Raw invite token is returned to the inviter; any admin can pre-register and own an account for any email |
| S4 | High | Custom API | `POST /api/integrations/custom-api/test` executes arbitrary requests with the org's stored credential, with no role, policy, approval, quota, or log |
| S5 | High | MCP mgmt tools | `sutr__install_integration` / `uninstall` / `start_oauth_flow` have no permission gate; a `viewer` can wipe or replace org integrations |
| S6 | High | Secrets | Default secrets backend stores every third-party token and API key in plaintext; JWT key sits on the same volume |
| S7 | High | Secrets (Vault) | Per-scheme credential refs omit `org_id`; with the Vault backend two orgs share one Vault path |
| S8 | High | Billing | Settlement payee supplies its own revenue share; overlapping periods double-credit |
| S9 | High | Google login | Google sign-in mints a session without the TOTP second factor |
| S10 | High | Connections | Provider OAuth callback is bound only to the initiating user, not the consenting browser (connection injection) |
| S11 | Medium | SSRF | `validate_safe_url` resolves DNS but httpx re-resolves; DNS rebinding reaches internal addresses |
| S12 | Medium | Marketplace | Subscribe ignores `organization` visibility; any tenant can subscribe to another's org-only tools |
| S13 | Medium | TOTP | Codes are replayable within the window; second-factor checks outside login have no rate limit; login TOTP failures never count toward account lockout |
| S14 | Medium | OAuth AS | Authorization code is not atomically single-use |
| S15 | Medium | OAuth AS | MCP access tokens live 7 days; unauthenticated `/register` is unbounded and lets the client pick the consent-page name |
| S16 | Medium | Config | `DEV=true` silently accepts the public default JWT secret |
| S17 | Medium | Resilience | Any tenant can reset the process-wide circuit breaker |
| S18 | Medium | Documentation | `.docx` parsing has no decompressed-size limit (zip bomb, inline in the request) |
| S19 | Medium | TOTP | TOTP seed stored in plaintext on the user row; recovery codes are 40-bit unsalted SHA-256 |
| S20 | High | UI / analytics | PostHog is initialised before auth pages strip tokens; session JWTs, reset/invite/verify tokens and OAuth codes are sent to PostHog in page URLs |
| S21 | High | SSRF | Remote MCP client and OAuth discovery follow redirects after the one-time URL check |
| S22 | Medium | Proxy | Hot-path tool execution reads the upstream response fully into memory with no size cap |
| S23 | Medium | UI | Open redirect via `?redirect=/\evil.com` on login, signup and verify-email |
| S24 | Medium | UI | Google callback accepts any `#access_token=` with no state binding (login CSRF / session fixation) |
| S25 | Medium | UI / OAuth | Consent page never shows the redirect URI; approve delivers the code through a sandboxed hidden iframe |
| S26 | Medium | CLI | API key written to a world-readable config file; server-supplied filename used verbatim for `openapi package` output |
| S27 | Low | Various | See "Low findings" |

---

## S1 — Critical: StreamableHTTP MCP sessions execute with the creating identity

**Files:** `server/src/sutr/mcp/asgi.py:196-214`, `server/src/sutr/mcp/server.py:46,83,102,120,181,244,328,372-376`
**SDK:** `mcp/server/streamable_http_manager.py:255-301` (mcp 1.29.1)

**Defect.** `mcp_asgi_app` authenticates the HTTP request, sets the `_current_auth` ContextVar, and hands off to `StreamableHTTPSessionManager` in stateful mode. On the first request (initialize) the SDK starts the long-lived session task with `task_group.start_soon(run_server)`, which copies the *initializing request's* context. Every later `tools/list` and `tools/call` on that session executes inside that task, so `_current_auth.get()` always returns the auth captured at session creation. Later requests must still present *some* valid credential, but the credential presented is never the one used for execution.

The SDK does have a session-owner check (`_session_owners`), but it only works when the ASGI `scope["user"]` is an `AuthenticatedUser`. Sutr never sets `scope["user"]`, so `requestor` is `None` for everyone and `None != None` is always False. The SSE transport (`mcp/sse.py:160-166`) implements exactly the tenant check that StreamableHTTP lacks.

```python
# asgi.py
auth_token = _current_auth.set(auth)
try:
    await session_manager.handle_request(scope, receive, send)   # existing session: handlers never see this auth
```
```python
# SDK: streamable_http_manager.py
user = scope.get("user")
requestor = authorization_context(user) if isinstance(user, AuthenticatedUser) else None
if requestor != self._session_owners.get(request_mcp_session_id):   # None != None -> never rejects
```

**Attack 1 — revocation bypass.** Attacker holds a leaked API key for org V. `POST /mcp` with `X-API-Key` + `initialize` yields `mcp-session-id: S`. Org V revokes the key. Attacker signs up a throwaway account (any valid credential), then sends `POST /mcp` with their own credential and `mcp-session-id: S`. The handlers run as `AgentAuth(org=V, api_key=<revoked>)`. Tools execute in org V indefinitely (idle timeout 1800 s is pushed back on each request).

**Attack 2 — cross-tenant session use.** Any authenticated principal who learns another tenant's `mcp-session-id` (logged at INFO by the SDK as "Created new transport with session ID", carried as a plain header through proxies) can drive that tenant's session, including `sutr__install_integration` / `sutr__uninstall_integration` which have no role check (S5).

**Fix.** Bind each session to the creating principal (api_key id or user id, plus org id) and reject requests whose freshly-authenticated principal differs, as `mcp/sse.py` does. Additionally re-resolve auth per request inside the handlers so revocation and membership changes take effect immediately. Setting `scope["user"]` to an `AuthenticatedUser` would also activate the SDK's own owner check.

---

## S2 — Critical: API keys bypass the whole role matrix

**Files:** `server/src/sutr/authz.py:117-131` (`ensure_agent_can`), `server/src/sutr/api/api_keys.py` (creation needs `api_keys:manage` = owner/admin/developer)
**Consumers:** `api/v1/billing.py` (`org:manage` at 213, 267, 287, 356, 372, 398, 459, 517, 535), `api/v1/governance.py` (`approvals:decide` at 231, 270, 472, 503, 594, 625, 640), `api/v1/registry.py:489`, `api/v1/provisioning.py:166,226,247,286,340`, `api/v1/resilience.py:358`, `api/v1/events.py:135`

**Defect.** `ensure_agent_can` returns early for API-key callers:

```python
def ensure_agent_can(session, auth, permission):
    if auth.user is None:
        return   # API key: no role, no check
```

The docstring frames this as "API keys keep their documented capabilities (install + call)", but every `/v1` router uses `get_agent_auth` + `ensure_agent_can` for *all* permissions including `org:manage` and `approvals:decide`. An `ApiKey` carries no role and no scopes.

**Attack.** A `developer` (cannot manage billing, cannot decide governance) calls `POST /api/api-keys` to mint a key, then with `X-API-Key`:
- `POST /v1/billing/plans`, `POST /v1/billing/invoices/{id}/void`, `POST /v1/settlements/run` (owner-only)
- `POST /v1/governance/reviews/{id}/decide {"approve": true}` (owner/admin-only)
- `POST /v1/registry/change-requests/{id}/decide`
- `POST /v1/provisioning/rules`

All succeed. The separation-of-duties checks are also written as `if actor_user_id is not None and author == actor: raise` (`governance/policies.py:313`, `governance/review.py:363`, `governance/exceptions.py:128`, `registry/service.py:451-455`), so with an API key the author approves their own policy/review/exception/change request.

**Fix.** Give API keys an explicit permission set (at minimum: scopes chosen at creation, capped at the creator's role) and make `ensure_agent_can` check it. Until then, reject API-key principals for any permission other than `tools:execute` and `integrations:manage` (the documented capabilities). SoD checks must treat "no user" as "not eligible to decide", not "exempt".

---

## S3 — Critical: Invite token returned to the inviter enables account pre-hijacking

**Files:** `server/src/sutr/api/orgs.py:303-341` (`create_invitation`), `server/src/sutr/api/orgs.py:425-501` (`accept_invitation`)

**Defect.** `create_invitation` returns `invite_url` containing the raw token to the inviter. `accept_invitation` is unauthenticated and, when no user exists for the invited email, creates one with `email_verified=True` and the password from the request body. The code comment states "Possession of the emailed invite token proves control of the mailbox", which is false because the inviter also holds it.

```python
invite_url = f"{settings.ui_base_url}/join?token={raw_token}"
...
return CreateInvitationResponse(**_serialize_invitation(inv).model_dump(), invite_url=invite_url)
```
```python
if user is None:
    user = User(email=inv.email, hashed_password=hash_password(body.password), email_verified=True)
```

**Attack (SaaS mode).** Attacker owns org A. `POST /api/org/invitations {"email":"ceo@victim.com","role":"member"}`, take the token from `invite_url`, then `POST /api/org-invitations/accept {"token":"...","password":"attacker-pw"}`. Result: a verified `ceo@victim.com` account whose password the attacker knows. Consequences:
- The real person gets `409 Email already registered` on signup.
- If they sign in with Google, `google_login.py:154-168` links their Google `sub` to the attacker-created row. Everything they later create or join is reachable by the attacker via password login. Silent, persistent takeover.
- The attacker can enable TOTP on the row to lock the victim out of password login while keeping the seed.

**Fix.** Do not return `invite_url` to the inviter in cloud mode (or only when no email backend is configured). Require a separate mailbox proof (verification code) before creating an account from an invite. Do not mark the account `email_verified` on invite acceptance.

---

## S4 — High: Custom API test endpoint executes with the org credential, ungated

**File:** `server/src/sutr/api/custom_api.py:395-474`

**Defect.** Every other mutating endpoint in the file calls `ensure_agent_can(..., "integrations:manage")`; `test_custom_api` calls nothing. It accepts a caller-supplied tool (any method, path, params) and, when `integration_db_id` is set and `base_url`/`token_header`/`token_format` match the stored row, loads the installed integration's secret and sends the request with it, returning the upstream body. The canonical pipeline (`services/tool_pipeline.py: evaluate_gate`) is never consulted, so per-tool `deny` / `require_approval` policy, quotas, the org rate limit, `LogEntry`, and usage metering are all skipped. The only trace is a `logger.info` line.

**Attack.** A `viewer` reads `id`, `base_url`, `token_header`, `token_format` from `GET /api/integrations/custom-api` (no role check), then:

```
POST /api/integrations/custom-api/test
Authorization: Bearer <viewer JWT>
{"base_url":"https://api.stripe.com","token_header":"Authorization","token_format":"Bearer {token}",
 "integration_db_id":"<row id>",
 "tool":{"name":"x","method":"DELETE","path":"/v1/customers/{id}","params":[{"name":"id","in":"path","type":"string"}]},
 "args":{"id":"cus_123"}}
```

The request goes out with the org's real Stripe key. The existing security test only covers the swapped-`base_url` case.

**Fix.** `ensure_agent_can(session, agent_auth, "integrations:manage")` at the top. Refuse to inject the stored secret unless the caller also holds `tools:execute`, and route the call through `evaluate_gate` so policy and logging apply.

---

## S5 — High: MCP management tools have no role gate

**File:** `server/src/sutr/mcp/management_tools.py:841-863` (`dispatch`), handlers `_handle_install`, `_handle_uninstall`, `_handle_start_oauth_flow`
**REST equivalents:** `api/installed.py:57,197,244` call `ensure_agent_can(..., "integrations:manage")`

**Defect.** `dispatch` routes straight to the handlers. The only MCP role check is `tools:execute` on `sutr__call_tool` / `sutr__await_approval` (`server.py:181,328`). A `viewer`, who cannot even execute tools, can mutate the org's integration set through MCP.

```
POST /mcp
Authorization: Bearer <viewer REST JWT>
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"sutr__uninstall_integration","arguments":{"integration_id":"github"}}}
```

This deletes the `InstalledIntegration`, the `OAuthState` row and its three secrets, writes an audit row attributed to the viewer, and pushes `tools/list_changed` to every session in the org. `sutr__install_integration` with `auth_method:"token"` makes the org's agents talk to the upstream as the attacker.

**Fix.** Call `ensure_agent_can(session, auth, "integrations:manage")` for install, uninstall and start_oauth_flow inside `dispatch`.

---

## S6 — High: Default secrets backend is plaintext

**Files:** `server/src/sutr/config.py:160` (`secrets_backend = "db"`), `server/src/sutr/secrets/db.py`, `docker-compose.prod.yml:46-50`, `deploy/ha/backup/backup.sh`

**Defect.** `DBSecretsBackend.store` returns the value unchanged and `retrieve` returns `secret.value`. Neither `docker-compose.prod.yml`, `fly.toml` (the hosted multi-tenant deployment), nor the HA compose/Helm set `SECRETS_BACKEND`, so every supported deployment path stores GitHub `repo` tokens, GCP `cloud-platform` refresh tokens, Azure/AWS tokens, Stripe keys and custom API keys in the clear. The prod compose also writes the generated `JWT_SECRET_KEY` to `/data/jwt_secret` on the same volume as the SQLite database, and the HA backup script emits dumps labelled `"encrypted": false`.

**Impact.** Any read of the database or a backup (copied volume, replica, operator laptop, SQL injection elsewhere) yields every tenant's upstream credentials plus the key to mint sessions for any user.

**Fix.** Make an encrypting backend the default (envelope encryption with a key from the environment, not the data volume). Fail startup in cloud mode if `secrets_backend == "db"`. Store the JWT secret outside the data volume.

---

## S7 — High: Vault credential refs are not org-scoped

**Files:** `server/src/sutr/credentials/store.py:96,112`, `server/src/sutr/credentials/resolve.py:181`, `server/src/sutr/secrets/vault.py:119-142`, `server/src/sutr/api/custom_api.py:81-95`

**Defect.** Every other `upsert_secret` caller includes `org_id` in `ref` (`oauth/{org}/{integration}/...`, `integrations/{org}/{integration}/token`, `deployments/{org}/{id}/token`). The `credentials/` module uses `f"{integration_id}:{scheme_name}"`. `integration_id` for custom APIs is `customapi_<slug>` allocated **per org**, so two orgs that each add "Stripe" both get `customapi_stripe`. With the Vault backend, `ref` is the Vault path (`sutr/customapi_stripe:apiKey`): `store` POSTs to it, `retrieve` GETs it, `delete` removes all versions.

**Attack (Vault backend).** Org B sets its API key for `customapi_stripe`. Org A's next tool call resolves credentials, reads the shared Vault path and sends **B's key** to A's configured `base_url`. B's key is exfiltrated to whatever host A controls, and A's calls act as B. Org B deleting its credential wipes A's. `scheme_name` also comes unsanitised from the uploaded OpenAPI `securitySchemes` key and is interpolated into the Vault URL path.

**Fix.** Include `org_id` in the ref, and URL-encode or whitelist `scheme_name`.

---

## S8 — High: Settlement payee picks its own revenue share; overlapping periods double-credit

**Files:** `server/src/sutr/api/v1/billing.py:455-473`, `server/src/sutr/billing/settlement.py:100-172,217-225`, `server/src/sutr/billing/invoices.py:108-113` vs `75-88`

**Defect 1.** `/v1/settlements/run` forces `provider_org_id` to the caller's org (the org being paid) but accepts `provider_share_bps` from that same caller and records a payable with it.

```python
share = default_share_bps() if provider_share_bps is None else provider_share_bps
provider_micros = round(gross * share / BPS)
```

`POST /v1/settlements/run {"period_start":"2026-01-01T00:00:00Z","period_end":"2026-02-01T00:00:00Z","provider_share_bps":10000}` credits the provider 100 % of gross.

**Defect 2.** The "already settled" guard uses `for_period`, which matches on identical `(period_start, period_end)`, while `_provider_totals` sums every invoice in the range. Running Jan 1–Feb 1 and then Jan 1–Feb 2 credits the same invoices twice. The same mismatch exists for invoices (`invoices.for_period` exact match vs `_usage_in_period` range), so overlapping invoice periods bill the same usage twice.

**Defect 3 (self-dealing loop, Medium).** `POST /v1/metering/events` accepts caller-supplied `tool_id`, settlement counts `ISSUED` (unpaid) invoices, and voiding an invoice reverses the invoice ledger entries but not the settlement credit. An attacker who owns a provider org and a consumer org can meter fictitious usage against their own tool, issue an invoice they never pay, settle 80–100 % of it into the provider's payable, then void the invoice.

**Fix.** Drop `provider_share_bps` from the tenant-facing request (platform-config only, or platform-admin only). Reject overlapping settlement/invoice periods, not just identical ones. Settle only `PAID` invoices, and reverse settlement credits on void.

---

## S9 — High: Google sign-in bypasses TOTP

**File:** `server/src/sutr/api/google_login.py:289-301`; compare `api/user_auth.py:128-150`

**Defect.** Password login treats `totp_enabled` as a real login factor. The Google callback mints an access token straight after `_find_or_create_user` with no second-factor check. A user with TOTP enabled whose Google account is compromised, or whose email gets linked per S3, is logged in with a full token. TOTP only protects the password path.

**Fix.** After `_find_or_create_user`, if `user.totp_enabled`, redirect to a TOTP challenge step instead of issuing the token.

---

## S10 — High: Provider OAuth callback not bound to the consenting browser (connection injection)

**Files:** `server/src/sutr/api/connections.py:189-344`

**Defect.** `authorize` stores `OAuthConnectState(user_id=<caller>)` and returns `authorization_url`. `oauth_callback` is unauthenticated, looks the row up by `state`, and saves the tokens under `pending.user_id` with no cookie or session binding to the browser that consented.

**Attack.** Attacker (any member with `integrations:manage` or `deployments:manage`) calls `POST /api/connections/gcp/authorize` and sends the returned Google consent URL to a victim within 15 minutes. The victim consents to "Sutr", and the victim's access/refresh tokens are stored on the **attacker's** `ProviderConnection`. The attacker then deploys into the victim's GCP/AWS/Azure account via `POST /api/deployments` (the `connection.user_id == agent_auth.user.id` check at `deployments.py:230` passes because the row is the attacker's) or reads the victim's private repos via `GET /api/connections/github/repos`.

**Fix.** Set a signed nonce cookie at `authorize` and require it on callback, or require the Sutr session on the callback.

---

## S11 — Medium: SSRF screen is check-then-fetch (DNS rebinding)

**Files:** `server/src/sutr/upstream_safety.py:35-47,79-86`; callers `source_connectors/url.py:110-125`, `openapi/loader.py:55-64`, `documentation/ingestion.py:121-131`, `api/custom_api.py`, `api_client.py`

**Defect.** `validate_safe_url` resolves the host and rejects private ranges, but the returned `resolved_ips` are never consumed (no caller reads them). httpx then resolves the hostname a second time. A DNS name that answers a public IP once and `127.0.0.1` / `169.254.169.254` next reaches internal services. `100.64.0.0/10` (CGNAT, e.g. Alibaba metadata `100.100.100.200`) and NAT64 `64:ff9b::/96` are also not covered. Redirects, `file://`, credentials-in-URL, fragments and size caps are handled correctly.

**Attack.** `POST /v1/sources/discover {"connector":"url","config":{"url":"http://rebind.attacker.tld/x"}}` or `POST /v1/documentation/jobs {"document_uri":"http://rebind.attacker.tld/x"}`; the fetched body (up to 25 MB) is stored and readable back via chunks.

**Fix.** Pin the connection to the validated IP (custom httpx transport that connects to the resolved address and sets `Host`), and add the missing ranges.

---

## S12 — Medium: Marketplace subscribe ignores `organization` visibility

**Files:** `server/src/sutr/marketplace/subscriptions.py:126-137`, `server/src/sutr/api/v1/marketplace.py:360-397`

**Defect.** `subscribable()` refuses only `VISIBILITY_PRIVATE`; `organization` falls through to "allowed". Listings and discovery hide org-only tools from other tenants, but `POST /v1/marketplace/subscriptions` loads the tool with `session.get(RegistryTool, body.tool_id)` (no org scope). Any org that knows the id can subscribe, which returns `tool_key`/`name`/`pricing_id`, records a subscription and a `subscription_created` event against the owner, and for unpublished tools leaks lifecycle state via the `ConflictError` message.

**Fix.** Treat `organization` visibility as subscribable only by members of the owning org.

---

## S13 — Medium: TOTP replay, unthrottled second-factor checks, and no lockout for TOTP failures

**Files:** `server/src/sutr/totp.py:66-71`, `server/src/sutr/api/second_factor.py`, `server/src/sutr/api/totp.py`, `server/src/sutr/api/user_auth.py:128-150`

- `verify_totp_code` uses `valid_window=1` and no "last used step" is persisted on `User`. A code seen once is valid for about 90 s and reusable across login, approval decisions, tool-setting escalation and TOTP disable. RFC 6238 §5.2 requires rejecting a reused step.
- `require_second_factor` (approvals, tool settings) and `_require_existing_second_factor` (TOTP disable / re-enable) have no rate limit and no failure counter. Three codes are valid at any instant, so an attacker holding a session JWT needs about 333k requests to disable 2FA or approve a gated tool call.
- At login, a TOTP failure calls `login_failure_ip_limiter.record(ip)` only. `failed_login_attempts` is not incremented, so the account lockout never engages for TOTP guessing; the only brake is the per-IP window, which an attacker rotates around.

**Fix.** Persist the last accepted time step and reject it again; count second-factor failures against the account lockout everywhere `verify_second_factor` is called.

---

## S14 — Medium: Authorization code is not atomically single-use

**File:** `server/src/sutr/mcp/oauth_provider.py:252-289`

`load_authorization_code` SELECTs the row; `exchange_authorization_code` SELECTs again and DELETEs without checking that it was the one to delete it, then mints tokens regardless. Two concurrent `POST /token` requests with the same code both succeed. **Fix:** `DELETE ... WHERE code=:code` and require `rowcount == 1`, else `invalid_grant`.

---

## S15 — Medium: Long-lived MCP access tokens and unbounded dynamic client registration

**Files:** `server/src/sutr/mcp/oauth_provider.py:68,125-154,306`, `server/src/sutr/config.py:23`, `server/src/sutr/main.py:579-586`, `server/src/sutr/api/oauth_server.py:390-394`

- `_issue_access_token` uses `settings.jwt_expire_minutes` (7 days) even though a 30-day refresh token is also issued. A leaked `mcp_access` bearer works for a week; combined with S1 it never stops working.
- `/register` is unauthenticated (required by spec) but has no rate limit, cap, expiry or cleanup; each call persists an `OAuthClient` and an encrypted `Secret`. The attacker-chosen `client_name` is what the consent page displays, so "Claude Desktop" can be registered for consent phishing.

**Fix.** Separate short TTL for MCP access tokens. Rate-limit `/register`, expire unused clients, and show the redirect host on the consent page.

---

## S16 — Medium: `DEV=true` accepts the default JWT secret

**Files:** `server/src/sutr/main.py:122-130`, `server/src/sutr/config.py:21`

`validate_startup_settings` returns early when `settings.dev` is set, and `dev` is otherwise only used to soften the schema check. An operator who sets `DEV=true` for schema leniency runs with the public key `change-me-in-production`, which signs sessions, impersonation tokens, MCP OAuth tokens and (via HKDF) access passes. **Fix:** make the guard unconditional, or limit the exception to loopback binds.

---

## S17 — Medium: Any tenant can reset the process-wide circuit breaker

**Files:** `server/src/sutr/api/v1/resilience.py:347-373`, `server/src/sutr/resilience/breaker.py:204-212`

`close_circuit` checks `org:manage` for the caller's org (bypassable by any API key per S2) but mutates the global `_circuits` shared by all tenants on the replica. `POST /v1/resilience/providers/<provider>/close` from any org re-opens a quarantined upstream for everyone.

---

## S18 — Medium: `.docx` zip bomb

**Files:** `server/src/sutr/documentation/parsing/docx_parser.py:72-83`, `server/src/sutr/documentation/ingestion.py:59`, `server/src/sutr/api/v1/documentation.py:244`

The 25 MiB limit applies to compressed bytes only. `archive.read("word/document.xml")` has no size guard and runs inline in the request, followed by `ElementTree.fromstring`. A 25 MiB deflate member expands to roughly 25 GiB. **Fix:** check `ZipInfo.file_size` before reading, cap it, and parse off the request thread.

---

## S19 — Medium: TOTP seed in plaintext; 40-bit recovery codes with unsalted SHA-256

**Files:** `server/src/sutr/models/user.py:45`, `server/src/sutr/totp.py:28-46`

`totp_secret` bypasses the secrets backend entirely. Recovery codes are `token_hex(3)[:5]` x 2 (40 bits) hashed with plain SHA-256; a database read gives full 2FA bypass for every user, and the recovery hashes fall to a GPU in minutes. **Fix:** store the seed via the secrets backend, use at least 80-bit recovery codes, or hash them with a KDF.

---

## S20 — High: Auth tokens are shipped to PostHog in page URLs

**Files:** `ui/src/analytics/posthog.ts:3-14`, `ui/src/main.tsx:11-18`, `ui/src/pages/GoogleCallbackPage.tsx:39-55`, `ui/src/pages/OAuthConsentPage.tsx:73-75`, `server/src/sutr/api/google_login.py:95-100`

**Defect.** PostHog is initialised unconditionally at module load with a hard-coded project token and `defaults: '2026-01-30'`, which enables `capture_pageview: "history_change"`. posthog-js captures the initial `$pageview` about 1 ms after load with `$current_url = window.location.href` unmasked. The Google callback lands on `/login/google/callback#access_token=<JWT>`; `GoogleCallbackPage` is lazy-loaded, so its `history.replaceState` runs long after the pageview fires. The same applies to `/reset-password?token=`, `/join?token=`, `/verify-email?token=`, `/oauth/authorize?session=` and `/oauth/success?redirect=<callback with code&state>`.

```ts
export const isPostHogEnabled = true
if (isPostHogEnabled) {
  posthog.init(posthogProjectToken, { api_host: posthogHost, defaults: '2026-01-30', person_profiles: 'identified_only' })
}
```

**Impact.** Anyone with access to the PostHog project (or a PostHog breach) receives live 7-day session JWTs, password-reset tokens and invite tokens. This applies to self-hosted installs too, since the token is hard-coded.

**Fix.** Do not place tokens in URLs (return a one-time code and exchange it via POST). Until then, initialise PostHog only after auth pages have stripped the URL, set `capture_pageview: false` on auth routes, and add a `sanitize_properties` hook that drops `token`, `session`, `redirect`, and the hash. Make analytics opt-in for self-hosted installs.

---

## S21 — High: Remote MCP client and OAuth discovery follow redirects after the one-time URL check

**Files:** `server/src/sutr/mcp/client.py:38-42,57,71,113`, `server/src/sutr/auth_start.py:188-193`, SDK `mcp/shared/_httpx_utils.py:31,70`

**Defect.** The user-supplied MCP server URL is screened once with `validate_safe_url`, then the connection follows redirects. The MCP SDK client factory forces `follow_redirects=True` ("always enabled" per its own comment), and `auth_start.py` explicitly constructs `httpx.AsyncClient(follow_redirects=True)` for the discovery probe, protected-resource metadata, authorization-server metadata and dynamic registration calls. A public URL that passes validation can answer `302 Location: http://169.254.169.254/...` and httpx opens it. The discovered authorization and token endpoints are also used without re-screening. httpx strips `Authorization` on cross-origin redirects, so this is SSRF-GET, but IMDSv1 and internal HTTP services are reachable. The plain REST proxy path in `api_client.py` correctly defaults `follow_redirects=False` and is not affected.

**Fix.** Pass a custom httpx client factory to the MCP SDK with `follow_redirects=False` (or an event hook that re-validates each hop), and re-screen every discovered endpoint URL before use.

---

## S22 — Medium: Tool execution buffers the upstream response with no cap

**Files:** `server/src/sutr/api_client.py:114-133,220-231,385-411`, `server/src/sutr/services/tool_pipeline.py:527-535`

`dispatch_api_tool` accepts `max_response_bytes` / `max_request_body_bytes` but defaults them to `None`, and the production hot path `call_tool` never passes them (its signature does not accept them). With `None`, `_read_response_body` does `await response.aread()` unbounded. Only the `/test` endpoint passes caps. A hostile or compromised upstream (the `base_url` is tenant-controlled) returning a multi-GB body OOMs the gateway worker for every tenant on that replica. **Fix:** thread a response cap through `call_tool` from the pipeline.

---

## S23 — Medium: Open redirect via backslash on login, signup and verify-email

**Files:** `ui/src/pages/LoginPage.tsx:46,58`, `ui/src/pages/SignupPage.tsx:25,52`, `ui/src/pages/VerifyEmailPage.tsx:84,122`

`navigate(redirect || '/app')` passes the raw `?redirect=` param to react-router. react-router 7.14 collapses `//` to `/` but not backslashes, and on the resulting cross-origin `pushState` SecurityError it falls back to `window.location.assign(url)`. `new URL("/\\evil.com", "https://app.sutr.sh/login")` resolves to `https://evil.com/`. A phishing link `https://app.sutr.sh/login?redirect=/%5Cevil.com` sends the user to evil.com after a genuine login. **Fix:** accept only paths matching `^\/(?![\/\\])`, or resolve with `new URL(redirect, origin)` and compare origins.

---

## S24 — Medium: Google callback accepts any `#access_token=` (login CSRF / session fixation)

**File:** `ui/src/pages/GoogleCallbackPage.tsx:39-58`

Any `#access_token=` in the fragment is stored with `setAuth(token)` and the user is sent to `/app/integrations`. There is no state or nonce binding the fragment to a flow this browser started. An attacker sends the victim `https://app.sutr.sh/login/google/callback#access_token=<attacker JWT>`; the victim is silently logged into the attacker's workspace, overwriting any existing session, and then pastes upstream API keys or connects GitHub/AWS into the attacker's org. **Fix:** set a nonce in `sessionStorage` before redirecting to Google, return it in the fragment and reject mismatches; better, exchange a one-time code via POST.

---

## S25 — Medium: Consent page hides the redirect URI; approve delivers the code through a sandboxed hidden iframe

**Files:** `ui/src/pages/OAuthConsentPage.tsx:72-78,86,115-120`, `ui/src/pages/OAuthSuccessPage.tsx:8,54`, `server/src/sutr/api/oauth_server.py:37-62`, `server/src/sutr/mcp/oauth_provider.py:125-146,231`

- The server returns `redirect_uri` in the consent session and the UI types it but never renders it. The only identity shown is `client_name || client_id`, which the dynamic-registration caller chooses (see S15). An attacker registers "Claude Desktop" with `redirect_uri=https://attacker.tld/cb`, starts `/authorize`, and sends the `/oauth/authorize?session=...` link to a victim, who sees "Client: Claude Desktop" with no destination and clicks Allow. The code goes to attacker.tld and is exchanged for a token to the victim's MCP tools.
- On approve, the server's `redirect_url` (containing `code` and `state`) is placed into `/oauth/success?redirect=...` (SPA URL, PostHog-captured per S20) and loaded in `<iframe sandbox="">`. Custom-scheme clients (`cursor://`, `vscode://`) never receive the code because Chrome blocks external-protocol navigation from a sandboxed frame, and https callbacks that rely on cookies or scripts run in an opaque, script-less frame. Deny uses top-level navigation; approve does not. The page also accepts an arbitrary `?redirect=` from anyone as a phishing surface.

**Fix.** Render the redirect host prominently and mark DCR clients as unverified. Use `window.location.replace(redirect_url)` on approve. Bind the consent session to the initiating browser.

---

## S26 — Medium: CLI config file permissions and output path traversal

**Files:** `cli/src/config.ts:50-53`, `cli/src/commands/openapi.ts:392-395`

- `~/.config/sutr/config.json`, which holds `api_key` (and legacy access/refresh tokens), is created with `mkdirSync(..., {recursive:true})` and `writeFileSync` with no mode, so it is 0644 under the default umask. `cli/src/connect/configure.ts:330-346` does chmod 600 for agent files but not for Sutr's own config. Any other local user on a shared box or CI runner reads the org-scoped key.
- When `--out` is omitted, `sutr openapi package` takes the output path verbatim from `Content-Disposition: filename="..."`. A malicious instance, or a MITM on an `http://` instance (which `checks.ts:67` allows), answers `filename="../../.bashrc"` and the CLI overwrites it.

**Fix.** `mkdirSync(CONFIG_DIR, { recursive: true, mode: 0o700 })`, `writeFileSync(CONFIG_PATH, data, { mode: 0o600 })`, and `basename()` plus a `^[\w.-]+\.zip$` check on the suggested filename.

---

## Low findings

- **`get_current_user` / `get_agent_auth`**: `uuid.UUID(user_id)` on the `sub` claim is unguarded; a malformed `sub` yields a 500 instead of a 401 (`dependencies.py:148,261`). Needs the signing key, so cosmetic.
- **Email verification link never expires** (`api/email_verification.py:61-70`): the link token is 256-bit and single-use, but the 30-minute promise in the email is not enforced and the burn-after-5-attempts path clears the code, not the link.
- **KMS envelope encryption uses no AAD** (`secrets/kms.py:34,50`): a database-write attacker can transplant org B's ciphertext + wrapped key onto org A's row. Bind AAD to `secret.id`/`org_id`.
- **`load_dotenv(override=True)`** (`config.py:4`): a stray `.env` beats orchestrator-injected secrets.
- **PostHog exception autocapture** (`analytics.py:10`): exception text, including `httpx.HTTPStatusError` URLs that may carry query-string API keys, leaves the box unredacted in cloud mode.
- **HA stack** (`deploy/ha/docker-compose.ha.yml`, `Caddyfile.ha`): default `sutr` DB password everywhere and plain `:80` with `TRUST_PROXY_HEADERS=true`. Labelled a demo.
- **`openapi_projects.py:92,369`**: `workspace_id` from the body is not checked against the caller's org (dangling cross-org pointer today, IDOR once workspace-scoped listing lands).
- **`marketplace.py:375-450`**: `upsert_review`/`delete_review` combine `get_agent_auth` and `get_current_user`; org A's API key plus org B's JWT writes a review in A attributed to a non-member.
- **Google login state** is not bound to the browser (no cookie), so classic login-CSRF is possible.
- **`/docs`, `/redoc`, `/openapi.json`** are exposed by default; no request body size limit in the app (relies on the proxy).
- **Platform-wide events** (`tenant_id IS NULL`) are retryable by any org with `org:settings:write` (`events.py:135-142`).
- **Generated `pyproject.toml`** (`openapi/packaging.py` ~1180, 1261, 1352): the spec's `info.title` is substituted into `description = "...__API_TITLE__..."` unescaped; a title with `"` and a newline injects TOML keys (for example a `[project.scripts]` entry point) into the generated package. Not direct RCE; the AST scanner only inspects `.py` files. All other API data goes through `json.dumps` into `tools.json`.
- **Docker provider** (`deploy/docker_provider.py:118-119`) passes runtime secrets as `-e KEY=value` subprocess args, visible in the process list and `docker inspect`. Acknowledged in the docstring; self-hosted only.
- **Impersonation** has no backend block on OAuth consent, API key creation, or org invites (`oauth_server.py:65-71` uses plain `get_current_user`), and the UI banner only renders inside the `/app` shell, not on `/oauth/authorize`, `/approve`, or `/join`.
- **OAuth popup** (`ui/src/components/connections/ConnectDialog.tsx:111`) is opened without `noopener`; a user-configured custom MCP authorization URL receives `window.opener` (reverse tabnabbing).
- **CLI** accepts `http://` instances (`cli/src/connect/checks.ts:67`) and `fetch` forwards the custom `X-API-Key` header across redirects (`cli/src/client.ts:148-154`).
- **Access token in `localStorage`** with no CSP in `ui/index.html`; admin and impersonation tokens share the same slot, so a failed `/admin/impersonate/stop` strands the admin.

---

## Verified as sound (for scope)

- JWT decoding pins HS256, checks `token_use`, rejects `/mcp`-audience tokens on REST and REST tokens are org-derived on MCP; revocation list and `token_version` are honoured on both.
- Password login: constant-time dummy bcrypt, per-IP window plus per-account lockout, generic errors; reset tokens hashed, 1 h TTL, bump `token_version`.
- Admin impersonation: admin-only, refuses admin targets, short TTL, revocable by hash, blocked from password/TOTP mutation.
- OAuth authorization server: PKCE S256 mandatory, exact `redirect_uri` match, code bound to client, refresh rotation, scope narrowing, audience check.
- Org roles: admins cannot grant/revoke owner, last owner protected, invitations single-use/expiring/hash-stored.
- Approvals: decisions gated by `approvals:decide` + TOTP; consume/transition are conditional UPDATEs so races resolve to one winner; args replayed from the stored normalised JSON.
- Tool policy defaults to `require_approval` when no setting exists.
- Outbound fetchers: http/https only, no credentials in URL, redirects refused, streamed size caps, GitHub/SwaggerHub hosts pinned.
- Deploy/CI: non-root images, SHA-pinned actions, no `pull_request_target`, trusted publishing with provenance, images signed and scanned.

---

## Remediation status (2026-10-17, uncommitted working tree)

All 26 findings and the Low items were addressed on the same day. Server suite: 2549 passed. CLI: 77 passed. UI: typecheck and production build clean. Alembic migration 0044 verified upgrade/downgrade/upgrade.

| # | Status | What changed |
|---|--------|--------------|
| S1 | Fixed | `mcp/asgi.py` sets `scope["user"]` so the SDK binds sessions to the creating principal; a second server-side `session_id -> principal` table rejects mismatches; handlers read per-request auth via `current_auth()` in `mcp/server.py`. SDK session-id log silenced. |
| S2 | Fixed | `ApiKey.role` column (migration 0044, default `developer`), set to the creator's role; `ensure_agent_can` checks it. Malformed `sub` is a 401. |
| S3 | Fixed | `invite_url` returned only when the server cannot email (`no RESEND_API_KEY` and not cloud); invite-created accounts are verified only when the token travelled by email. |
| S4 | Fixed | `/custom-api/test` requires `integrations:manage`; stored secret injected only with `tools:execute`. Not yet routed through the policy gate. |
| S5 | Fixed | `management_tools.dispatch` enforces `integrations:manage` for install, uninstall and start_oauth_flow. |
| S6 | Fixed | New default backend `db_encrypted` (`secrets/local.py`, AES-256-GCM, AAD = ref, key from `SECRETS_ENCRYPTION_KEY` or HKDF of the JWT secret). `db` refused in cloud mode. Legacy plaintext rows re-encrypted once at startup. Compose keeps keys on a separate `sutr_secrets` volume. |
| S7 | Fixed | Credential refs are `credentials/{org}/{integration}/{scheme}`; Vault path segments are percent-encoded. |
| S8 | Fixed | Caller-supplied share is a 422; overlapping settlement and invoice periods refused; only PAID invoices settle; void writes a compensating debit for any settled share. |
| S9 | Fixed | Google callback issues a 5-minute `totp_challenge` token for TOTP users; `POST /api/auth/totp-challenge` completes login; UI renders the step. |
| S10 | Fixed | `authorize` sets an HMAC cookie bound to `state`; callback requires it. |
| S11 | Fixed | `pinned_request_target` resolves once and connects to the vetted IP with `Host` plus SNI; applied to the proxy, spec loader, URL connector and documentation fetch. Blocklist extended (CGNAT, NAT64, mapped/compatible IPv6, `is_global`). |
| S12 | Fixed | `organization` visibility subscribable only by the owning org; foreign tools return 404 with no lifecycle leak. |
| S13 | Fixed | `totp_last_used_step` rejects replayed steps; every second-factor failure counts toward account lockout; counters reset only after both factors pass. |
| S14 | Fixed | Atomic `DELETE ... WHERE code` with rowcount check before minting. |
| S15 | Fixed | `MCP_ACCESS_TOKEN_EXPIRE_MINUTES` (default 60); `/register` rate-limited per IP; consent session exposes `dynamically_registered`. Unused-client sweep deferred (needs a `last_used_at` column). |
| S16 | Fixed | Default JWT secret accepted only with `DEV=true` and a loopback `BASE_URL`, with a warning. |
| S17 | Fixed | `close_circuit` restricted to platform admins. |
| S18 | Fixed | `.docx` parts capped at 64 MiB and 200:1 compression ratio before read. |
| S19 | Fixed | TOTP seed stored through the secrets backend (`totp_secret_id`, lazy migration from the legacy column); recovery codes are 64-bit. |
| S20 | Fixed | PostHog initialises only after auth routes have stripped the URL, with `capture_pageview: false`, path-only manual pageviews and a sanitiser; key from `VITE_PUBLIC_POSTHOG_KEY`, cloud fallback only when `VITE_IS_CLOUD`. Fragment captured synchronously before React loads. |
| S21 | Fixed | MCP client factory refuses 3xx; OAuth discovery no longer follows redirects and screens every discovered URL. |
| S22 | Fixed | `TOOL_RESPONSE_MAX_BYTES` (10 MiB) and a 4 MiB request-body cap on the hot path; truncation surfaced in the result. |
| S23 | Fixed | `safeInternalPath` guards every `?redirect=` navigation. |
| S24 | Fixed | Nonce generated by the UI, stored per tab, echoed by the server in the fragment and required on the callback page. |
| S25 | Fixed | Consent page shows the redirect host and an "Unverified client" badge; approve uses top-level `location.replace`; success page is static. |
| S26 | Fixed | Config dir 0700 and file 0600; download filename sanitised; `X-API-Key` requests refuse redirects; warning for non-loopback `http://` instances. |
| Low | Fixed | KMS AAD bound to ref (legacy fallback), `load_dotenv(override=False)`, platform events retry admin-only, cursor 400, noopener on the connect popup, impersonation banner on standalone pages. Not changed: HA demo defaults, `/docs` exposure, docker provider `-e` args, pyproject title escaping. |

Follow-ups still open: route `/custom-api/test` through `evaluate_gate`; add `oauth_client.last_used_at` and sweep stale dynamic clients; escape `info.title` in generated `pyproject.toml`; move the cloud PostHog key out of source via a build arg.
