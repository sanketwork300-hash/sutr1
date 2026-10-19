# Sutr — Logic / Correctness Audit Findings

Date: 2026-10-17
Scope: same as `SECURITY_BUGS.md`. This file lists defects whose primary effect is wrong behaviour (money, state machines, control flow, data integrity) rather than an attacker gaining access. Where a logic bug is also the root cause of a security finding, the security id is cross-referenced and the full write-up lives in `SECURITY_BUGS.md`.

Severity here means: **High** = wrong money, lost data, or a broken user-facing flow. **Medium** = incorrect behaviour under a realistic condition. **Low** = incorrect but rarely hit or cosmetic.

---

## Summary table

| # | Severity | Area | Title |
|---|----------|------|-------|
| L1 | High | Billing | "Already settled / already invoiced" guards match exact periods only; overlapping periods double-count |
| L2 | High | Billing | Settlements credit unpaid (`ISSUED`) invoices and voiding an invoice never reverses the settlement credit |
| L3 | High | UI auth | Every 401 is treated as "session expired": a wrong password hard-reloads `/login`, the error never renders, and `?redirect=` is lost |
| L4 | High | OAuth consent UI | Approve delivers the authorization code through a sandboxed hidden iframe; custom-scheme and cookie-dependent clients never receive it |
| L5 | High | AuthZ | Separation-of-duties checks are written as "if there is a user and user == author", so a `None` actor (API key) is exempt (see S2) |
| L6 | Medium | MCP transport | SDK session-owner check is inert because `scope["user"]` is never populated (see S1) |
| L7 | Medium | OAuth AS | Authorization code exchange is select-then-delete, not atomic; concurrent exchanges both mint tokens (see S14) |
| L8 | Medium | Marketplace | `subscribable()` enumerates `private` and `archived` but lets `organization` visibility fall through to "allowed" (see S12) |
| L9 | Medium | Login | TOTP failures only hit the per-IP limiter; `failed_login_attempts` is never incremented, so account lockout does not apply to the second factor (see S13) |
| L10 | Medium | Google login | A Google account whose email changed overwrites the local email; collision with another local account raises on the unique index and returns a generic failure |
| L11 | Medium | SSRF guard | `validate_safe_url` computes `resolved_ips` and no caller ever uses them, so the check protects nothing against a second DNS answer (see S11) |
| L12 | Medium | Proxy | `call_tool` cannot pass a response cap; the only caller with caps is `/test` (see S22) |
| L13 | Medium | Resilience | Circuit breaker state is process-global but exposed and authorised per tenant (see S17) |
| L14 | Medium | Quotas | Quota evaluation is read-then-allow; concurrent calls overshoot the limit (acknowledged in the docstring) |
| L15 | Medium | Pricing | Tiered plans accept negative `amount_micros`; `validate()` checks only tier ordering, so `issue()` later fails at `ledger.append` |
| L16 | Low | Events API | `int(cursor["id"])` on an untrusted pagination cursor raises `ValueError` and returns 500 |
| L17 | Low | PDP | When tenant passes and both RBAC and ABAC abstain (API-key principal, no rules) the decision is `allowed=True` |
| L18 | Low | Email verification | Link token never expires although the email promises 30 minutes; burning the code after 5 bad attempts leaves the link valid |
| L19 | Low | Login ordering | Email-verification check runs before TOTP; `verify-email-code` issues a full access token, so an unverified TOTP-enabled user logs in without TOTP |
| L20 | Low | Auth deps | `uuid.UUID(sub)` is unguarded in `get_current_user` and `get_agent_auth`; a malformed `sub` is a 500 not a 401 |
| L21 | Low | Deployments | `update` and `rollback` do not gate on deployment `status`; two concurrent updates queue two revisions |
| L22 | Low | Config | `load_dotenv(override=True)` lets a stray `.env` beat injected environment variables |
| L23 | Low | DB | SQLite engine never sets `PRAGMA foreign_keys=ON`; FK cascades are not enforced on SQLite installs |
| L24 | Low | install.sh | `--help` uses `sed "$0"`, which fails under `curl | sh` |

---

## L1 — High: Exact-match period guards allow overlapping double-counting

**Files:** `server/src/sutr/billing/settlement.py:108-116,217-225` (`for_period`) vs `66-72` (`_provider_totals`); `server/src/sutr/billing/invoices.py:108-113` (`for_period`) vs `75-88` (`_usage_in_period`)

**Defect.** The guard that stops a period from being settled or invoiced twice looks up a record whose `(period_start, period_end)` are **identical** to the request:

```python
existing = for_period(session, provider_org_id=..., start=period_start, end=period_end)   # exact match
if existing is not None and existing.state == STATE_COMPLETE:
    raise ConflictError("This period is already settled...")
```

But the totals are computed over a **range**: every invoice (settlement) or usage event (invoice) whose timestamp falls inside the window.

**Failure.** Run a settlement for Jan 1–Feb 1, then again for Jan 1–Feb 2. The second call finds no record with those exact bounds, sums the same `ISSUED`/`PAID` invoices again, and appends a second `KIND_SETTLEMENT` credit. The provider's payable is doubled. Invoices behave the same way: Jan 1–31 then Jan 1–30 prices the Jan 1–30 usage on both invoices. This is reachable by any caller with `org:manage` (or any API key, S2) and also by an honest operator correcting a period boundary.

**Fix.** Reject any new period that overlaps an existing `COMPLETE` settlement / `ISSUED` invoice for the same org (`new.start < existing.end AND new.end > existing.start`), or mark usage events and invoices as consumed by a specific settlement/invoice id and exclude already-consumed rows.

---

## L2 — High: Settlements credit unpaid invoices and void does not reverse them

**Files:** `server/src/sutr/billing/settlement.py:66-89`, `server/src/sutr/billing/invoices.py:242-264`, `server/src/sutr/api/v1/billing.py:56-70,143-162`

**Defect.** `_provider_totals` counts invoices in state `ISSUED` and `PAID`. `void()` reverses the invoice's own ledger entries but not any settlement credit that was computed from it. `POST /v1/metering/events` accepts a caller-supplied `tool_id` with no check that the tool belongs to a provider the caller actually bought from.

**Failure.** Consumer org C meters 1,000,000 calls against provider org P's tool, issues an invoice and never pays. P runs a settlement and is credited 80 % (or 100 %, S8) of that gross as a payable. C voids the invoice. P's payable stays. An operator paying payables off-platform pays for revenue that never existed. The same sequence can happen innocently when a disputed invoice is voided after month-end settlement.

**Fix.** Settle only `PAID` invoices. On void (and on refund), write a compensating `DEBIT` against any settlement that included the invoice, or re-open that settlement.

---

## L3 — High: Any 401 reloads the login page, swallowing the error and the redirect

**Files:** `ui/src/api/client.ts:64-68`, `ui/src/pages/LoginPage.tsx:55-58`, `server/src/sutr/api/user_auth.py:54-59`

```ts
if (res.status === 401) {
  clearToken()
  window.location.href = '/login'
  throw new Error('Unauthorized')
}
```

`/api/auth/token` returns 401 for a wrong password, and `api.auth.login` goes through the same `request()` wrapper. A typo therefore triggers a hard navigation to `/login`: `setError` never paints, and the `?redirect=` query used by the MCP OAuth consent flow (`/login?redirect=/oauth/authorize?session=...`) and by `JoinPage` is discarded. After one typo the OAuth flow dead-ends at `/app` and the MCP client never gets its code. The TOTP step is unaffected because it returns 403.

**Fix.** Only redirect on 401 when a bearer token was attached and the path is not `/auth/token`; otherwise throw `ApiError` so the page can render it.

---

## L4 — High: OAuth approve via sandboxed hidden iframe never reaches many clients

**Files:** `ui/src/pages/OAuthConsentPage.tsx:72-78`, `ui/src/pages/OAuthSuccessPage.tsx:8,54`, `server/src/sutr/mcp/oauth_provider.py:146,231`

On approve, the SPA navigates to `/oauth/success?redirect=<server redirect_url>` and loads it with `<iframe src={redirectUrl} sandbox="" style="display:none">`. Chrome blocks external-protocol navigation from a sandboxed frame, so `cursor://`, `vscode://`, `windsurf://` callbacks never fire. `https` callbacks that rely on first-party cookies or scripts (for example claude.ai's `/api/mcp/auth_callback`) run in an opaque-origin, script-less frame and cannot match `state`. Only loopback `http://localhost:*` clients work. The backend accepts any `AnyUrl` as `redirect_uri`, so these clients are admitted and then silently fail. Deny already uses top-level `window.location.href`.

**Fix.** Use `window.location.replace(redirect_url)` for approve; keep the iframe only for loopback URIs if at all.

---

## L5 — High: Separation-of-duties checks exempt a `None` actor

**Files:** `server/src/sutr/governance/policies.py:313`, `server/src/sutr/governance/review.py:363`, `server/src/sutr/governance/exceptions.py:128`, `server/src/sutr/registry/service.py:451-455`

Every four-eyes check has the shape:

```python
if actor_user_id is not None and version.authored_by_user_id == actor_user_id:
    raise ForbiddenError(...)
```

The guard was written for "anonymous actor" but the only path that produces `None` is an API key, which `ensure_agent_can` already exempts from the role check. The result is that the author of a policy, review, exception or registry change request approves it themselves by switching from a JWT to an API key for the decide call. The policy module docstring calls its SoD check "unconditional". Full write-up and exploit path: S2.

**Fix.** Treat `actor_user_id is None` as "not eligible to decide" (raise), not as "exempt".

---

## L6 — Medium: SDK session-owner binding is inert

**Files:** `server/src/sutr/mcp/asgi.py:196-214`, SDK `mcp/server/streamable_http_manager.py:255-275`

The SDK rejects a request whose `requestor` differs from the session's owner, but computes `requestor` from `scope["user"]` only when it is an `AuthenticatedUser`. Sutr authenticates in its own ASGI wrapper and never sets `scope["user"]`, so every session has owner `None`, every requestor is `None`, and the check never fires. Separately, the handlers read `_current_auth` from the ContextVar captured when the session task was spawned, not from the current request. Full write-up: S1.

---

## L7 — Medium: Authorization code exchange is not atomic

**File:** `server/src/sutr/mcp/oauth_provider.py:252-289`

`load_authorization_code` SELECTs; `exchange_authorization_code` SELECTs again, deletes if present, and mints tokens whether or not the delete happened. Two concurrent exchanges both succeed. See S14.

---

## L8 — Medium: `subscribable()` falls through for `organization` visibility

**File:** `server/src/sutr/marketplace/subscriptions.py:126-137`

```python
if tool.visibility == VISIBILITY_PRIVATE: return False, "..."
if tool.lifecycle_state == ARCHIVED: return False, "..."
if tool.lifecycle_state not in SUBSCRIBABLE_STATES: return False, "..."
return True, None     # "organization" reaches here for any caller
```

Listings and discovery hide `organization` tools from other tenants; subscribe does not. See S12.

---

## L9 — Medium: TOTP failures never count toward account lockout

**File:** `server/src/sutr/api/user_auth.py:128-150`

On a wrong password the handler increments `failed_login_attempts` and sets `locked_until` at the threshold. On a wrong TOTP code it only calls `login_failure_ip_limiter.record(ip)`. The account-level brake, which is the one an IP-rotating attacker cannot dodge, never engages for the second factor. See S13.

---

## L10 — Medium: Google email sync can collide with another local account

**File:** `server/src/sutr/api/google_login.py:144-152`

```python
user = session.exec(select(User).where(User.google_sub == sub)).first()
if user:
    if user.email != email:
        user.email = email      # "Google is authoritative"
```

If the Google account's email has changed to an address that already exists as a separate local Sutr user, the commit violates `user_email_lower_uq`. The exception is not caught in `_find_or_create_user`, so the callback falls into the generic `login_failed` redirect and the user is locked out of Google login with no actionable message. The sync also changes a user's login identity without re-verification or audit.

**Fix.** Check for an existing row with the new email before updating; on conflict keep the old email and surface a specific error.

---

## L11 — Medium: `resolved_ips` is dead data

**File:** `server/src/sutr/upstream_safety.py:18,79-86`

`SafeUpstreamURL.resolved_ips` is populated from `getaddrinfo` and then never read anywhere (grep confirms the only reference is its definition). Every connection is opened by hostname and httpx resolves again. The DNS check is therefore advisory only. See S11.

---

## L12 — Medium: Response cap exists but the hot path cannot use it

**File:** `server/src/sutr/api_client.py:114-133,385-411`

`dispatch_api_tool(max_response_bytes=None)` falls back to `await response.aread()`. `call_tool`, the only production caller via `tool_pipeline.execute_tool`, has no parameter for the cap and passes nothing. See S22.

---

## L13 — Medium: Circuit breaker is global but authorised per tenant

**Files:** `server/src/sutr/api/v1/resilience.py:347-373`, `server/src/sutr/resilience/breaker.py:204-212`

`_circuits` is keyed by provider, not by `(org, provider)`. `close_circuit` checks the caller's own `org:manage` and then mutates the shared state for every tenant on the replica. See S17.

---

## L14 — Medium: Quota is read-then-allow

**File:** `server/src/sutr/services/quota.py:186-260`

`evaluate` sums `usage_event` rows and compares to the limit; the usage row for this call is written after execution. N concurrent calls at `used = limit - 1` all pass. The module docstring acknowledges per-process concurrency only. The in-process `in_flight` counter covers `CONCURRENT_TOOL_CALLS` but not the daily/monthly counters.

---

## L15 — Medium: Negative tier prices pass validation and break invoicing

**File:** `server/src/sutr/billing/pricing.py` (`_tiered`, `validate`)

`rate = int(tier.get("amount_micros", 0))` accepts negatives; `validate()` checks tier ordering and the open last tier only. A negative subtotal reaches `issue()`, which fails at `ledger.append` (refuses negative amounts), leaving the invoice half-built in the session until rollback. Self-inflicted by the pricing org, but the error surfaces at invoice time, far from the cause.

---

## L16 — Low: Pagination cursor parsing returns 500

**File:** `server/src/sutr/api/v1/events.py:82,110`

`int(page_request.cursor["id"])` on an untrusted cursor raises `ValueError` and surfaces as a 500. `common/pagination.py` decodes defensively elsewhere; this caller bypasses that.

---

## L17 — Low: PDP default-allow when all layers abstain

**File:** `server/src/sutr/provisioning/pdp.py:256-263`

With an API-key principal (no role) and no ABAC rules, tenant passes and RBAC/ABAC both ABSTAIN; the combined decision is `allowed=True`. Documented as "no rules is no opinion", but it means an unbound API key with no rules obtains access passes for any tool via `POST /v1/provisioning/passes`. Combine with S2.

---

## L18 — Low: Verification link never expires

**Files:** `server/src/sutr/api/email_verification.py:61-70`, `server/src/sutr/email/verification.py:114-118`

The `GET /verify-email?token=` path checks only `email_verification_token_hash`, never `email_verification_code_expires_at`. Burning the 6-digit code after 5 failures clears the code hash but not the link token. The email template promises 30 minutes.

---

## L19 — Low: Login ordering lets the verification-code path skip TOTP

**Files:** `server/src/sutr/api/user_auth.py:118-150`, `server/src/sutr/api/email_verification.py:76-92`

`login` checks email verification (403 with a `verification_token`) **before** it checks TOTP. `verify-email-code` then returns a full access token with no TOTP check. A TOTP-enabled user who is unverified (possible when `SKIP_EMAIL_VERIFICATION` was on when they enrolled and later turned off) logs in with password + emailed 6-digit code, never the authenticator. Low because the precondition is unusual.

---

## L20 — Low: Unguarded `uuid.UUID(sub)`

**File:** `server/src/sutr/dependencies.py:148,261`

A validly-signed token with a non-UUID `sub` raises `ValueError` and returns 500 instead of the 401 that every other malformed-token path returns. Requires the signing key; cosmetic.

---

## L21 — Low: Deployment update/rollback do not gate on status

**File:** `server/src/sutr/api/deployments.py` (update / rollback handlers), `server/src/sutr/services/deployments.py:195-220`

Two concurrent updates each open a revision and both are applied by the provider in arrival order; `next_revision_number` and `_settle_revision` keep the history consistent, but the second update can win while the UI shows the first as current. Not a security issue.

---

## L22 — Low: `.env` overrides the environment

**File:** `server/src/sutr/config.py:4`

`load_dotenv(override=True)` means a stray `.env` in the working directory beats `JWT_SECRET_KEY`, `DATABASE_URL`, and `SECRETS_BACKEND` injected by Helm or compose. The default (`override=False`) is the safer behaviour for a service.

---

## L23 — Low: SQLite foreign keys are not enforced

**File:** `server/src/sutr/db.py`

The engine never issues `PRAGMA foreign_keys=ON` on connect, so `ON DELETE CASCADE` declared in the models and migrations is ignored on SQLite. Deleting an org, user or integration leaves orphan rows on self-hosted installs while PostgreSQL cascades correctly. Add an `engine` connect event that sets the pragma.

---

## L24 — Low: `install.sh --help` under `curl | sh`

**File:** `install.sh`

The help path runs `sed "$0"`; when the script is piped from curl, `$0` is `sh` and the command fails. Everything else in the script (set -eu, no sudo, chmod 600 on `.env`, random JWT secret) is sound.
