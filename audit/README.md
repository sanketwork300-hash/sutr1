# Sutr Codebase Audit — 2026-10-17

Two reports, split by the nature of the defect:

| File | Contents |
|------|----------|
| [`SECURITY_BUGS.md`](SECURITY_BUGS.md) | 26 ranked security findings (3 Critical, 9 High, 14 Medium) plus Low items, what was verified as sound, and a remediation-status table (all 26 fixed on 2026-10-17) |
| [`LOGIC_BUGS.md`](LOGIC_BUGS.md) | 24 logic / correctness findings (5 High, 10 Medium, 9 Low); cross-references the security ids where a logic bug is the root cause |

## The ones to fix first

1. **S1 — MCP sessions execute as the identity that created them.** `scope["user"]` is never set, so the SDK's owner check is inert and handlers read a ContextVar captured at session creation. A session id is cross-tenant access, and revoked keys keep working. (`mcp/asgi.py`, `mcp/server.py`)
2. **S2 / L5 — API keys bypass the whole role matrix and every four-eyes check.** `ensure_agent_can` returns early for API keys, and every `/v1` route (billing, governance, provisioning, registry) relies on it. A developer mints a key and acts as owner. (`authz.py:117-131`)
3. **S3 — Invite token returned to the inviter.** Any org admin can create a verified account for any email with a password they choose, then wait for the real person to link Google to it. (`api/orgs.py`)
4. **S4 — `POST /api/integrations/custom-api/test`** runs arbitrary requests with the org's stored upstream credential with no role, policy, approval, quota or log. (`api/custom_api.py:395-474`)
5. **S6 / S7 — Secrets.** Plaintext storage by default on every shipped deployment path, and the Vault backend shares one path between orgs for custom-API credentials. (`secrets/db.py`, `credentials/store.py`)
6. **S8 / L1 / L2 — Billing money bugs.** Payee chooses its own share, overlapping periods double-credit, unpaid invoices settle, void does not reverse. (`billing/settlement.py`, `billing/invoices.py`)
7. **S20 — PostHog receives session JWTs and reset/invite tokens** from page URLs because analytics initialises before auth pages strip them. (`ui/src/analytics/posthog.ts`)

## Method

- Full manual read of `dependencies.py`, `authz.py`, `auth_tokens.py`, all user-auth endpoints, the TOTP and verification modules, `config.py`, startup validation, and the canonical tool pipeline (`services/tool_pipeline.py`, `approvals/*`).
- Six parallel reviews, each reading its area in full: REST API routers and models; `/v1` routers and the billing/governance/provisioning/registry backends; MCP transport and OAuth authorization server; outbound proxy, SSRF guard, codegen and deploy providers; secrets, credentials, connections, models and deploy config; UI and CLI.
- Every finding in both files was re-verified against the source by the orchestrating reviewer before inclusion. Things that looked suspicious but turned out to be handled are listed under "Verified as sound" in `SECURITY_BUGS.md` so the coverage is explicit.
- No exploit code was written and no changes were made to application code.
