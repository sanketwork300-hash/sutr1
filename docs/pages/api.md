---
title: API Reference
---
# API Reference

The server runs on port `4747` by default. Interactive docs (Swagger UI) are available at `http://localhost:4747/docs`.

All endpoints except `/api/users/register`, `POST /api/auth/token`, `GET /api/auth/verify-email`, `POST /api/auth/verify-email-code`, `POST /api/auth/resend-verification-code`, `POST /api/auth/resend-verification-by-email`, `GET /api/auth/callback`, `GET /api/auth/google/login`, `GET /api/auth/google/callback`, `GET /api/connections/{provider}/callback`, and `GET /api/integrations` require authentication. Pass a bearer token in every request:

```
Authorization: Bearer <token>
```

---

## Users

### `POST /api/users/register`

Create a new user. Automatically creates an organization and makes the user its owner. No auth required.

When `IS_SELF_HOSTED=true`, the first successful registration becomes the admin (`is_admin=true`) and
all subsequent registrations return `409` (one org per self-hosted instance).

**Body:**
```json
{
  "email": "alice@example.com",
  "password": "s3cr3t",
  "org_name": "Acme Corp"
}
```

**Response `201`:**
```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "org_id":  "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "email":   "alice@example.com",
  "email_verification_required": true,
  "verification_token": "eyJhbGciOiJIUzI1NiIs...",
  "resend_available_at": "2026-04-17T20:20:07.149028+00:00"
}
```

---

## Auth

### `POST /api/auth/token`

Log in and get a JWT bearer token. Uses OAuth2 password form encoding (`application/x-www-form-urlencoded`). No auth required.

**Form fields:**
- `username` — the user's email address
- `password` — the user's password
- `totp_code` — required when the user has 2FA enabled: a 6-digit authenticator code or a recovery code. Without it the endpoint returns `403` with `{"detail": {"error": "totp_required"}}`; a wrong code returns `403` with `{"detail": {"error": "totp_invalid"}}`.

**Response `200`:**
```json
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer"
}
```

**Response `403` when email verification is still pending:**
```json
{
  "detail": {
    "error": "email_verification_required",
    "message": "Enter the 6-digit verification code we sent to your email.",
    "email": "alice@example.com",
    "verification_token": "eyJhbGciOiJIUzI1NiIs...",
    "resend_available_at": "2026-04-17T20:20:07.149028+00:00"
  }
}
```

### `POST /api/auth/logout`

Revoke the presented bearer token server-side. Returns `204` (idempotent). Password changes and resets also invalidate every outstanding token for the account.

### `GET /api/auth/verify-email`

Verify an email from the link sent by email. No auth required.

**Query params:** `token`

**Response `200`:**
```json
{
  "message": "Email verified successfully",
  "email": "alice@example.com"
}
```

### `POST /api/auth/verify-email-code`

Verify an email using the 6-digit code plus the temporary `verification_token` returned by signup or unverified login. No auth required.

After 5 wrong code attempts the current code is burned: further submissions (including the correct code) will fail until a new code is requested via `POST /api/auth/resend-verification-code`. Both `GET /api/auth/verify-email` and `POST /api/auth/verify-email-code` are additionally capped at 10 requests per minute per client IP — excess requests receive `429 Too Many Requests`.

**Body:**
```json
{
  "code": "123456",
  "verification_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

**Response `200`:**
```json
{
  "message": "Email verified successfully",
  "access_token": "eyJhbGci...",
  "token_type": "bearer"
}
```

### `POST /api/auth/resend-verification-code`

Resend the verification email using the temporary `verification_token`. No auth required.

You can only resend once every 10 minutes — unless the previously issued code was burned by too many wrong attempts, in which case the cooldown is bypassed so the user can recover.

**Body:**
```json
{
  "verification_token": "eyJhbGciOiJIUzI1NiIs..."
}
```

**Response `200`:**
```json
{
  "message": "Verification email sent",
  "resend_available_at": "2026-04-17T20:30:07.149028+00:00"
}
```

**Response `429`:**
```json
{
  "detail": {
    "error": "verification_email_rate_limited",
    "message": "Verification email already sent recently. Try again later.",
    "resend_available_at": "2026-04-17T20:30:07.149028+00:00"
  }
}
```

### `POST /api/auth/{integration_id}/start`

Begin an OAuth flow for an installed integration. Requires auth.

**Response:**
```json
{
  "authorization_url": "https://app.posthog.com/oauth/authorize?...",
  "state": "abc123"
}
```

Open `authorization_url` in a browser. After the user authorizes, the provider redirects to the callback URL and tokens are stored automatically.

### `GET /api/auth/callback`

OAuth redirect target. Handled automatically by the server — do not call directly.
On success, the browser is redirected to the integration detail page in the UI at `/app/integrations/{integration_id}`.

**Query params:** `code`, `state` (set by the OAuth provider)

### `GET /api/auth/google/login`

Begin a "Sign in with Google" flow for a user logging in to sutr itself. No auth required.
Completely independent from the Google integration's OAuth — set `GOOGLE_LOGIN_CLIENT_ID` and
`GOOGLE_LOGIN_CLIENT_SECRET` to enable.

**Response `200`:**
```json
{ "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?..." }
```

The UI redirects the browser to `authorization_url`. Returns `503` if Google login is not configured.

### `GET /api/auth/google/callback`

Google redirects here after the user approves. Handled automatically — do not call directly.
On success, the browser is redirected to `${UI_BASE_URL}/login/google/callback#access_token=<jwt>&token_type=bearer`.
On failure, the browser is redirected to `${UI_BASE_URL}/login?google_error=<code>`.

**Query params:** `code`, `state`, `error` (set by Google)

If a user with the returned Google account's email already exists, that account is linked (and kept signed
in with their existing password if any). Otherwise a new user + org are created, subject to
`BLOCK_SIGNUPS` and `IS_SELF_HOSTED` rules.

---

## Public Config

### `GET /api/config`

Returns non-sensitive runtime config used by the UI. Public — no auth required.

**Response `200`:**
```json
{
  "is_self_hosted": true,
  "billing_enabled": false
}
```

---

## Integrations

Browse the catalog of bundled integrations. Public — no auth required.

### `GET /api/integrations`

List all available bundled integrations.

**Query params:**
- `type` — filter by integration type (`remote_mcp`, `custom`)

**Response:**
```json
[
  {
    "id": "posthog",
    "name": "PostHog",
    "type": "remote_mcp",
    "description": "Product analytics and feature flags",
    "docs_url": "https://posthog.com/docs/model-context-protocol",
    "url": "https://mcp.posthog.com/mcp",
    "auth": [
      { "method": "oauth" },
      { "method": "token", "label": "PostHog Personal API Key", "header": "Authorization", "format": "Bearer {token}" }
    ]
  }
]
```

### `GET /api/integrations/{id}`

Get a single bundled integration by ID (e.g. `github`, `posthog`).

### Custom API definitions

Custom API integrations are org-scoped REST integrations backed by declarative `ApiTool`
definitions. They use token-in-header authentication only; the token itself is stored through
the normal install flow, not on the definition.

#### `GET /api/integrations/custom-api`

List custom API definitions for the current org.

#### `POST /api/integrations/custom-api`

Create a custom API definition.

**Body:**
```json
{
  "name": "Example API",
  "description": "Internal customer data",
  "base_url": "https://api.example.com",
  "token_header": "Authorization",
  "token_format": "Bearer {token}",
  "tools": [
    {
      "name": "get_customer",
      "description": "Fetch one customer",
      "method": "GET",
      "path": "/v1/customers/{id}",
      "params": [
        { "name": "id", "type": "string", "required": true }
      ]
    }
  ]
}
```

#### `GET /api/integrations/custom-api/{id}`

Return the full definition, including tools. `{id}` is the custom API definition UUID, not
the catalog `integration_id`.

#### `PATCH /api/integrations/custom-api/{id}`

Update definition fields. If the integration is installed, changes to `base_url`, auth header,
auth format, or tools refresh the tool cache and notify live MCP sessions.

#### `DELETE /api/integrations/custom-api/{id}`

Delete a definition. Returns `409` until the matching installed integration is uninstalled.

#### `POST /api/integrations/custom-api/test`

Run one draft `ApiTool` against the live upstream before saving. The submitted `token` is used
only for this request and is not persisted.

**Body:**
```json
{
  "base_url": "https://api.example.com",
  "token_header": "Authorization",
  "token_format": "Bearer {token}",
  "token": "sk_test_...",
  "tool": {
    "name": "get_customer",
    "description": "Fetch one customer",
    "method": "GET",
    "path": "/v1/customers/{id}",
    "params": [{ "name": "id", "required": true }]
  },
  "args": { "id": "cus_123" }
}
```

**Response `200`:**
```json
{
  "content": [{ "type": "text", "text": "{\n  \"id\": \"cus_123\"\n}" }],
  "isError": false,
  "status_code": 200,
  "duration_ms": 184
}
```

---

## Connected accounts

OAuth grants Sutr holds on your behalf: GitHub for reading OpenAPI specifications out of
repositories, and Google Cloud / Azure / AWS for running generated MCP servers in your own
account. See [Connected accounts](/connected-accounts) for the concepts and
[the setup guide](/self-host/connected-accounts) for registering the OAuth apps.

A connection is scoped to one **user** inside one organization. An API key cannot open one —
it belongs to the organization, not to a person, so the connection would have no identity to
revoke. GitHub connections require `integrations:manage`; cloud connections require
`deployments:manage`. Tokens are stored through the secrets backend and never returned.

### `GET /api/connections`

Every provider this build knows, whether the server has it configured, and who is connected.

```json
{
  "providers": [
    {
      "id": "github", "display_name": "GitHub", "kind": "source",
      "flow": "authorization_code", "scopes": "repo read:user",
      "configured": true, "reason": null,
      "setup_url": "https://github.com/settings/developers",
      "grant_summary": "Read the repositories you can already see, ...",
      "callback_url": "http://localhost:4747/api/connections/github/callback",
      "connection": {
        "id": "…", "provider": "github", "account_label": "octocat",
        "scopes": "repo", "expires_at": null, "expired": false,
        "metadata": {}, "created_at": "…", "updated_at": "…"
      }
    }
  ]
}
```

`configured: false` carries a `reason` naming the exact environment variables and callback URL
the operator must set — an unconfigured provider is the operator's missing setup, not a broken
button.

### `POST /api/connections/{provider}/authorize`

Begin a connection. `provider` is `github`, `gcp`, `azure`, or `aws`.

**Body:** `{"redirect_after": "/connections/callback"}` — a **relative** UI path to return to.
Anything else is discarded rather than sanitized.

For the authorization-code providers:

```json
{"flow": "authorization_code", "authorization_url": "https://github.com/login/oauth/authorize?…",
 "grant_summary": "…"}
```

For AWS, which uses the OAuth 2.0 device grant (RFC 8628) against IAM Identity Center:

```json
{"flow": "device", "state": "…", "user_code": "ABCD-EFGH",
 "verification_uri": "https://device.sso.us-east-1.amazonaws.com/",
 "verification_uri_complete": "https://…?user_code=ABCD-EFGH",
 "interval": 5, "expires_in": 600, "grant_summary": "…"}
```

`503` with `{"error": "provider_not_configured"}` when the server has no OAuth app for it.

### `GET /api/connections/{provider}/callback`

Where the provider redirects the browser. **No auth** — the `state` parameter is the only thing
binding the code to a user, so the state row is single-use, expires after 15 minutes, and is
deleted before the code is exchanged whatever the outcome. Always responds `302` back into the
UI with `connection`, `connection_status` (`connected` | `denied` | `error`), and on failure
`connection_error`.

### `POST /api/connections/aws/poll`

One step of the AWS device grant. **Body:** `{"state": "…"}`.

Returns `{"status": "pending"}` while the user has not approved yet — that is a normal state of a
flow in progress, not an error — and `{"status": "connected", "connection": {…}}` once Identity
Center issues the token. Only the Identity Center token is stored; every AWS operation exchanges
it for short-lived role credentials.

### `GET /api/connections/github/repos?q=`

Repositories the connected GitHub account can read, most recently updated first, filtered by `q`.
`409` with `{"error": "not_connected"}` when there is no GitHub connection.

### `GET /api/connections/{connection_id}/targets`

Where a cloud connection can deploy: Google Cloud projects, Azure subscriptions, or AWS accounts
(each with the Identity Center roles assigned to you in it).

```json
{"provider": "aws", "targets": [
  {"id": "111122223333", "label": "Prod (111122223333)", "detail": "ops@acme.com",
   "roles": ["AdministratorAccess", "ReadOnly"]}
]}
```

`400` for a source provider — GitHub has no deploy targets.

### `DELETE /api/connections/{connection_id}`

Deletes the stored tokens. **Does not revoke the app** on the provider's side; only the user can
do that from the provider's own settings. `204`.

CLI equivalents: `sutr connections list|connect|targets|disconnect`.

---

## OpenAPI projects

Import an OpenAPI 3.x document and compile it into a custom API integration. The generated tools flow through the same install/discovery/approval/execution pipeline as every other integration.

### `POST /api/openapi/import`

Import a specification. Requires `integrations:manage`.

**Body:**
```json
{
  "name": "Petstore",
  "source_kind": "paste",
  "content": "{ \"openapi\": \"3.0.3\", ... }"
}
```

`source_kind` is one of `paste`, `upload`, `url`, `github`, or `swaggerhub`.

- **`paste` / `upload`** — the document arrives in `content`. For `upload`, pass `filename` too: it is kept as the project's provenance, which is the only record a local import has of where it came from.
- **`url`** — a direct link to the document. SSRF-screened (no private, loopback, or link-local targets), redirects are not followed, and the response is size-capped.
- **`github`** — `url` is any GitHub repository, tree, blob, or `raw.githubusercontent.com` link. If it does not already name a file, Sutr picks the best candidate itself (see `/discover` below). Pass `path` to choose a specific file. For a private repository, either set `use_connection: true` to authorize with the caller's [connected GitHub account](/connected-accounts), or pass `github_token` — an explicit token always wins, and is used for that one request and never stored.
- **`swaggerhub`** — `url` is a SwaggerHub API URL (`app.`, `portal.`, or `api.swaggerhub.com`, in `/apis/{owner}/{api}/{version}` form). Pass `swaggerhub_api_key` for a private API; SwaggerHub expects the key as-is, so Sutr sends it unprefixed rather than as a Bearer token. `resolved` (default `true`) asks SwaggerHub to inline `$ref`s before returning the document.

Provider credentials are pinned to their provider's host: a `github_token` is only ever attached to a request whose host already resolved to GitHub, and likewise for SwaggerHub. In every case the document is validated against the official OpenAPI 3.0/3.1 schemas, `$ref`s are resolved cycle-safely, external `$ref`s are refused, and Swagger 2.0 is rejected.

**Response (201):** the project with `api_title`, `api_version`, `operation_count`, per-operation summaries, discovered `tags`, `servers`, `security_schemes`, `suggested_auth` translated from the spec's security schemes, `source_url`, and — for `github`/`swaggerhub` — a `provenance` object recording exactly which owner, repository, branch, and path the document came from.

### `POST /api/openapi/discover`

List the specification files in a GitHub repository before importing one. Requires `integrations:manage`.

**Body:** `{"url": "https://github.com/owner/repo", "use_connection": false, "github_token": "<optional>"}`

Resolves the repository's default branch when the URL names none, walks the tree once, and returns the spec-shaped files it found:

```json
{
  "source_kind": "github",
  "owner": "swagger-api", "repo": "swagger-petstore", "branch": "master",
  "candidates": [
    {"path": "src/main/resources/openapi.yaml", "filename": "openapi.yaml", "size": 23165}
  ]
}
```

Candidates are ordered best-first: `openapi.*` before `swagger.*`, shallower paths before deeper ones, YAML before JSON, and merely spec-shaped filenames last. The first entry is a *default selection*, not a decision — pass whichever `path` you want to `/import`. A `/tree/` URL scopes the search to that subdirectory. When the repository contains no specification the call fails with `no_spec_found` and names the paths it searched.

### `GET /api/openapi` / `GET /api/openapi/{project_id}`

List projects / full project detail.

### `POST /api/openapi/{project_id}/compile`

Compile the project into tools. Requires `integrations:manage`.

**Body:**
```json
{
  "dry_run": true,
  "filters": {
    "include_tags": [], "exclude_tags": ["admin"],
    "include_paths": [], "exclude_paths": ["/internal/*"],
    "include_operations": [], "exclude_operations": [],
    "include_deprecated": false
  },
  "server_url": "https://api.example.com/v1",
  "server_variables": {"environment": "api"},
  "auth": {"token_header": "X-Api-Key", "token_format": "{token}"},
  "integration_name": "Petstore Tools"
}
```

With `dry_run: true` the response previews the exact tools (deterministic names from `operationId`, documented collision handling), warnings, resolved base URL, and auth — nothing is created. Without it, a custom API integration is created (or updated in place on recompile) and appears in the catalog; connect and set per-tool approval policies as usual. The base URL is SSRF-validated at compile time and again on every call.

### `POST /api/openapi/{project_id}/package`

Generate a **standalone MCP server package** (zip) for the project. Accepts the same body as `/compile` (`dry_run` is ignored). Requires `integrations:manage`.

The zip is a self-contained Python project with no Sutr dependency: `server.py` (MCP stdio server), `sutr_runtime.py` (request building/execution mirroring the gateway's semantics), `tools.json` (the compiled tool definitions — pure data), generated offline tests (`test_server.py`), `Dockerfile`, `README.md`, and `.env.example`. Credentials are read only from an environment variable named in the bundle; agent-supplied headers can never override the credential header. The base URL is deliberately **not** SSRF-screened for packages — they run on your own infrastructure, where private-network APIs are legitimate targets. Output is byte-deterministic; each generation is recorded in the audit trail.

### `DELETE /api/openapi/{project_id}`

Delete the import artifact. A compiled integration lives on and is managed through the custom-API endpoints.

CLI equivalents: `sutr openapi discover|import|list|show|compile|package|delete`. `sutr openapi import --url` accepts GitHub and SwaggerHub links directly; `--path` picks a file inside a repository, and credentials come from `--github-token` / `--swaggerhub-key` or the `SUTR_GITHUB_TOKEN` / `SUTR_SWAGGERHUB_API_KEY` environment variables.

---

## Deployments

Run generated MCP servers on a deployment provider. Four ship:

| Provider | What it creates | Authorized by |
|---|---|---|
| `docker` | A container on the Sutr host, bound to `127.0.0.1` on an ephemeral port | nothing — the host's own daemon |
| `gcp` | Artifact Registry repo (if missing) → Cloud Build build → public Cloud Run service | a `gcp` connected account |
| `azure` | Resource group, registry, environment (each if missing) → ACR Tasks build → Container App | an `azure` connected account |
| `aws` | S3 bucket and ECR repo (if missing) → CodeBuild build → App Runner service | an `aws` connected account |

Every provider serves MCP at `/mcp` and a health probe at `/health`. The cloud providers build the
image with that cloud's own build service and run it on its serverless container runtime, scaling
to zero when idle; because they act with the user's own OAuth grant, a deployment can only ever
touch what that user could already touch.

`docker` is disabled on cloud (multi-tenant) instances and can be turned off with
`DEPLOY_DOCKER_ENABLED=false`. Each cloud provider can be turned off with
`DEPLOY_GCP_ENABLED` / `DEPLOY_AZURE_ENABLED` / `DEPLOY_AWS_ENABLED`, and reports itself
unavailable — with the missing variables named — when its OAuth app is not configured.

All mutations require `deployments:manage` (owner/admin/developer) and are audited. The optional upstream `token` is stored through the secrets backend and injected as an environment variable at run time — never baked into the image or the package.

### `POST /api/deployments`

```json
{
  "project_id": "<openapi project uuid>",
  "name": "Petstore prod",
  "provider": "gcp",
  "connection_id": "<provider connection uuid>",
  "provider_config": {"project": "acme-prod", "region": "us-central1"},
  "token": "sk_live_...",
  "compile": { "filters": {"exclude_tags": ["admin"]}, "server_url": "https://api.example.com" }
}
```

`connection_id` is required for every provider that declares a `connection_provider`, and must be
a connection belonging to the caller — a connection is a personal grant, so one member cannot
deploy under another's cloud identity.

`provider_config` carries the placement. Its keys come from the provider's own `config_fields`
(see `GET /api/deployments/providers`), so a new provider needs no client change. Declared
defaults are filled in and stored alongside the values you sent, so the recorded config is the
whole truth and a later stop, start, or delete addresses the same target. Missing required fields
are rejected before anything is created, and the provider's readiness is probed with real
credentials — "ready" means ready, not merely "configured".

Returns `201` with `status: "queued"`; the build runs in the background
(`queued → building → running | failed`). A cloud build takes a few minutes. Poll
`GET /api/deployments/{id}`.

### Other endpoints

- `GET /api/deployments` / `GET /api/deployments/{id}` — list/detail; stored status is reconciled with the provider's live state. Includes `connection_id`, the stored `config`, and `console_url` (a deep link into the cloud's own console) where there is one.
- `GET /api/deployments/providers` — availability plus, for each provider, `connection_provider`, a one-sentence `creates`, and the `config_fields` a client must collect (each with `kind`, `required`, `default`, and `help`).
- `GET /api/deployments/{id}/logs?tail=100` — logs. Cloud Run reads Cloud Logging and App Runner reads CloudWatch; **Azure Container Apps keeps application logs in Log Analytics**, a different API with a different token audience than this grant covers, so it returns a portal link instead of an empty string.
- `POST /api/deployments/{id}/stop` / `/start` — lifecycle. Container Apps and App Runner have real stop/pause operations. Cloud Run has no stopped state — it already scales to zero — so *stop* switches ingress to internal-only, which makes it genuinely unreachable without destroying the revision.
- `DELETE /api/deployments/{id}` — removes what belongs to that deployment and deletes the stored token. Shared resources (resource group, registry, environment, bucket, ECR repository) are deliberately left alone.

Every provider operation resolves credentials fresh: an OAuth access token refreshed if due, or —
for AWS — the Identity Center token exchanged for short-lived role credentials.

CLI equivalents: `sutr deploy providers|list|create|status|logs|stop|start|delete`.
`sutr deploy create --provider gcp --connection <id> --config project=acme --config region=us-central1`.

---

## Installed integrations

Manage configured instances of integrations, scoped to your organization.

### `GET /api/installed`

List all installed integrations for your org.

### `POST /api/installed`

Install an integration.

**Body:**
```json
{
  "integration_id": "posthog",
  "auth_method": "token",
  "token": "phx_..."
}
```

- `auth_method` — `"token"` or `"oauth"`
- `token` — required when `auth_method` is `"token"`

**Response:** `201` with the installed integration object.

### `DELETE /api/installed/{integration_id}`

Remove an installed integration by ID.

---

## Tools

### `GET /api/tools`

List tools across all installed integrations in your org. Each tool includes an `execution_mode` field indicating whether it can be called freely or requires approval.

**Response:**
```json
[
  {
    "integration_id": "posthog",
    "name": "get_events",
    "description": "...",
    "input_schema": {},
    "execution_mode": "require_approval"
  }
]
```

### `GET /api/tools/{integration_id}`

List tools for a single installed integration. Each tool includes its `execution_mode`.

### `POST /api/tools/{integration_id}/call`

Call a tool on an installed integration.

**Body:**
```json
{
  "tool_name": "get_events",
  "args": {
    "project_id": 1234,
    "event": "$pageview"
  },
  "additional_info": "Checking recent pageviews to decide whether to roll out the new landing page."
}
```

- `additional_info` — optional free-text explanation the agent can attach to justify the call. Shown to humans reviewing the approval request and persisted on the log entry. It is **never** forwarded to the upstream tool, so it cannot conflict with tool argument schemas. Omit the field entirely when not needed.

When calling tools via the MCP endpoint (`/mcp`), pass the same value as an `additional_info` property inside the tool-call `arguments`; the server strips it from the arguments before forwarding.

**Response `200` (allowed):**
```json
{
  "content": [ { "type": "text", "text": "..." } ],
  "isError": false
}
```

**Response `403` (approval required):**

When a tool requires approval and no matching policy exists, the call is blocked:

```json
{
  "error": "approval_required",
  "approval_request_id": "550e8400-...",
  "approval_url": "/approve/550e8400-...",
  "message": "Tool call requires approval before execution.",
  "integration_id": "posthog",
  "tool_name": "get_events"
}
```

The agent should present the `approval_url` to the user. After the user approves, the agent retries the same call.

If you want the server to hold the wait instead of sleeping in the client, use
`POST /api/tool-approvals/requests/{request_id}/await`. The CLI wraps this as
`ap tools await-approval`, and `ap tools call --wait` uses it automatically.

---

## Tool Settings

Manage per-tool execution modes. By default, all tools require approval (`require_approval`).

### `GET /api/tool-settings/{integration_id}`

List execution settings for tools on the given installed integration.

**Response:**
```json
[
  {
    "id": "...",
    "org_id": "...",
    "integration_id": "github",
    "tool_name": "create_issue",
    "mode": "allow",
    "updated_by_user_id": "...",
    "updated_at": "2026-04-09T12:00:00"
  }
]
```

### `PUT /api/tool-settings/{integration_id}/{tool_name}`

Set execution mode for a specific tool.

**Body:**
```json
{
  "mode": "allow"
}
```

Valid modes: `allow`, `require_approval`, `deny`. Setting a tool to `allow` requires a fresh TOTP code when 2FA is enabled. Changing modes requires the `policies:write` permission (owner/admin).

---

## Organization

Org lifecycle, members, and invitations. Roles: `owner`, `admin`, `developer`, `member`, `viewer`.

| Permission | Roles |
|---|---|
| Rename org, billing | owner |
| Manage members & invitations | owner, admin |
| Org settings, workspaces | owner, admin |
| Change tool policies, decide approvals | owner, admin |
| Manage integrations & API keys | owner, admin, developer |
| Execute tools | owner, admin, developer, member |
| Read logs & tools | all roles |

### `GET /api/org`

Current org's `id`, `name`, and your `role`.

### `PATCH /api/org`

Rename the org (owner only). Body: `{"name": "Acme Corp"}`.

### `GET /api/org/members`

List members: `[{"user_id", "email", "role", "is_you"}]`.

### `PATCH /api/org/members/{user_id}`

Change a member's role. Body: `{"role": "developer"}`. Only owners may grant or revoke `owner`; an org must always keep at least one owner (409 otherwise).

### `DELETE /api/org/members/{user_id}`

Remove a member (owner/admin), or leave the org yourself. The last owner cannot be removed.

### `GET /api/org/invitations` · `POST /api/org/invitations` · `DELETE /api/org/invitations/{id}`

Manage invitations (owner/admin). `POST` body: `{"email": "x@example.com", "role": "member"}`. The response includes a one-time `invite_url` (`/join?token=...`) — also emailed when an email backend is configured. Invitations expire after 7 days; only the token's SHA-256 hash is stored.

### `GET /api/org-invitations/preview?token=...`

Public. Returns `{org_name, email, role, account_exists}` for a pending invitation.

### `POST /api/org-invitations/accept`

Public (the token is the credential). Body: `{"token": "...", "password": "..."}` — `password` is required only when no account exists for the invited email; a fresh account is created email-verified and the response includes an `access_token`. Accepting never creates a new org, so invitations work on single-org self-hosted installs.

---

## Workspaces

Sub-divisions of an org. Every org has a non-deletable default workspace.

### `GET /api/workspaces` · `POST /api/workspaces` · `PATCH /api/workspaces/{id}` · `DELETE /api/workspaces/{id}`

List is available to all members; mutations require owner/admin. `POST`/`PATCH` body: `{"name": "Production APIs"}`. Deleting the default workspace returns 409.

---

## Org Settings

Per-org runtime settings.

### `GET /api/org-settings`

Read the current org's settings.

**Response:**
```json
{
  "approval_expiry_minutes": 30,
  "approval_expiry_minutes_default": 10,
  "approval_expiry_minutes_override": 30
}
```

`approval_expiry_minutes_override` is `null` when the org is using the instance default.

### `PATCH /api/org-settings`

Update the org's settings. Only fields present in the body are applied; pass `null` to revert a field to its default.

**Body:**
```json
{
  "approval_expiry_minutes": 30,
  "log_retention_days": 90
}
```

`approval_expiry_minutes` must be between 1 and 1440. The new value applies to approval requests created after the change; existing pending requests keep their original expiry.

---

## Tool Approvals

Manage approval requests for blocked tool calls.

### `GET /api/tool-approvals/requests`

List approval requests for your org.

**Query params:**
- `status` — filter by status (`pending`, `approved`, `denied`, `expired`, `consumed`)
- `integration_id` — filter by integration
- `tool_name` — filter by tool
- `limit` — max results (default `50`, max `500`)
- `offset` — pagination offset (default `0`)

### `GET /api/tool-approvals/requests/{request_id}`

Get a single approval request by ID.

**Response:**
```json
{
  "id": "550e8400-...",
  "org_id": "...",
  "integration_id": "github",
  "tool_name": "create_issue",
  "args_json": "{\"title\":\"Bug report\"}",
  "args_hash": "a1b2c3...",
  "summary_text": "Run github.create_issue with arguments ...",
  "status": "pending",
  "requested_at": "2026-04-09T12:00:00",
  "expires_at": "2026-04-10T12:00:00",
  "decision_mode": null,
  "decided_at": null,
  "additional_info": "Opening this issue to track the auth regression I just reproduced."
}
```

`additional_info` is the optional rationale supplied by the caller at the time of the tool call, or `null`.

### `POST /api/tool-approvals/requests/{request_id}/await`

Agent-facing long-poll endpoint for approval decisions. This is the REST equivalent of the MCP
`sutr__await_approval` flow, except it returns the current approval status and lets the caller
retry the original tool call once the status becomes `approved`.

**Body (optional):**
```json
{ "timeout_seconds": 30 }
```

If omitted, the server waits for up to `approval_long_poll_timeout_seconds` (default `240`).
Larger requested values are capped to that server-side maximum.

**Response:**
```json
{
  "approval_request_id": "550e8400-...",
  "integration_id": "github",
  "tool_name": "create_issue",
  "status": "approved",
  "message": "Approved. Retry the original tool call to execute it.",
  "expires_at": "2026-04-10T12:00:00",
  "decision_mode": "approve_once"
}
```

Possible `status` values include `pending`, `approved`, `denied`, `expired`, `consumed`, and `auto_approved`.

### `POST /api/tool-approvals/requests/{request_id}/approve-once`

Approve a pending request for one-time use. The agent must retry the call to consume the approval.

**Body:**
```json
{ "totp_code": "123456" }
```
`totp_code` is only required when the approving user has TOTP enabled (see `/api/users/me/totp`). Pass either a 6-digit authenticator code or an unused recovery code.

**Response:** The updated approval request with `status: "approved"` and `decision_mode: "approve_once"`.

Returns `409` if already decided. Returns `410` if expired. Returns `403` with `{"detail": {"error": "totp_required" | "totp_invalid", ...}}` when the user has TOTP enabled and the code is missing or wrong.

### `POST /api/tool-approvals/requests/{request_id}/approve-exact`

Approve a pending request for these **exact arguments, forever**. Future calls whose normalized argument hash matches execute without a new approval and are logged with `access_reason: "approved_exact"`; any change to the arguments goes back through the approval gate. The grant is never consumed and does not expire.

Accepts the same optional `totp_code` body as `approve-once`.

**Response:** The updated approval request with `decision_mode: "approve_exact_forever"`.

### `POST /api/tool-approvals/requests/{request_id}/allow-tool`

Approve a pending request and allow all future calls to this tool regardless of arguments.

Accepts the same optional `totp_code` body as `approve-once`.

**Response:** The updated approval request with `decision_mode: "allow_tool_forever"`.

### `POST /api/tool-approvals/requests/{request_id}/deny`

Deny a pending request.

Accepts the same optional `totp_code` body as `approve-once`.

**Response:** The updated approval request with `status: "denied"`.

---

## Audit trail

### `GET /api/audit`

Control-plane audit events for the organization: logins, password/TOTP changes, API-key lifecycle, per-tool policy changes (with old and new mode), approval decisions, integration installs/uninstalls, membership and invitation changes, and admin impersonation. Requires the `audit:read` permission (owner or admin).

Query params: `action`, `target_type`, `target_id`, `limit` (max 500), `offset`.

```json
[
  {
    "id": 12,
    "timestamp": "2026-08-19T12:00:00",
    "action": "policy.mode_changed",
    "actor_type": "user",
    "actor_user_id": "6ba7b810-...",
    "target_type": "tool_policy",
    "target_id": "posthog/create_annotation",
    "summary": "create_annotation on posthog: require_approval -> allow",
    "metadata_json": "{\"old_mode\": \"require_approval\", \"new_mode\": \"allow\"}",
    "ip": "203.0.113.7"
  }
]
```

Audit events are append-only and exempt from log retention. Tool-call logs (`/api/logs`) are stored with credential-shaped values redacted (`[REDACTED]`) and can be pruned by setting `log_retention_days` in `PATCH /api/org-settings`.

---

## Usage & metering

Every tool execution is metered into a durable ledger (`usage_event`) in the same transaction as its log entry, so an execution can never be logged without being metered — or metered without having run. Calls stopped by policy (denied, or gated pending approval) consumed no upstream work and are **not** metered; they remain in the logs and audit trail. Deployment runtime accrues in 5-minute samples while a deployment is observed running. Retention never prunes this table.

Both endpoints work with a session JWT **or** an API key (the CLI and SDKs use keys), and are org-scoped.

### `GET /api/usage/summary?start=&end=`

Defaults to the last 30 days; the window may not exceed 366 days. Timestamps may be offset-aware (e.g. `2026-08-19T12:00:00Z`) or naive UTC.

```json
{
  "start": "2026-07-20T12:00:00", "end": "2026-08-19T12:00:00",
  "tool_calls": 1284,
  "totals_by_kind": [{"kind": "tool_call", "quantity": 1284, "events": 1284},
                     {"kind": "deployment_runtime", "quantity": 8640, "events": 1728}],
  "tool_calls_by_outcome": [{"outcome": "executed", "count": 1250}, {"outcome": "error", "count": 34}],
  "tool_calls_by_source": [{"source": "mcp", "count": 900}, {"source": "api", "count": 384}],
  "top_integrations": [{"integration_id": "posthog", "count": 512}],
  "top_tools": [{"integration_id": "posthog", "tool_name": "create_annotation", "count": 300}],
  "daily": [{"date": "2026-08-18", "count": 61}],
  "duration_ms": {"avg": 284.3, "max": 4120}
}
```

`deployment_runtime` quantity is **minutes**, not events.

### `GET /api/usage/events?start=&end=&kind=&limit=&offset=`

Raw metered events for export and reconciliation. Usage events carry counts and dimensions only — never arguments or results.

CLI: `sutr usage summary [--days N]`, `sutr usage events [--kind ...]`.

---

## Metrics & tracing

### `GET /metrics`

Prometheus exposition. **Disabled by default** — set `METRICS_ENABLED=true`. When `METRICS_TOKEN` is also set, scrapers must send `Authorization: Bearer <token>`; while disabled the endpoint returns 404 rather than advertising itself.

Series are process-level and aggregate: `sutr_tool_calls_total{source,outcome}`, `sutr_tool_call_duration_seconds`, `sutr_tool_calls_gated_total{source,reason}`, `sutr_approval_decisions_total{decision}`, `sutr_http_requests_total{method,route,status}`, `sutr_http_request_duration_seconds`, and `sutr_deployments{status}`.

They deliberately carry **no org, integration, or tool labels** — that would explode cardinality and leak tenant identifiers to whoever scrapes the endpoint. Per-tenant numbers live in the usage ledger above. HTTP series are labelled with the matched route *template* (`/api/tools/{integration_id}/call`), never the raw path.

### Tracing

Optional OpenTelemetry export, off by default. Install the extra (`uv sync --extra otel`) and set `OTEL_ENABLED=true` plus `OTEL_EXPORTER_OTLP_ENDPOINT` (spans are recorded locally if the endpoint is omitted). Tool executions emit a `sutr.tool_call` span with integration, tool, source, access reason, outcome, and duration attributes — never arguments, results, or credentials, since traces leave the process.

---

## Two-factor authentication (TOTP)

When enabled, every approval decision (approve-once, allow-tool, deny) requires a fresh authenticator code from the approving user. The secret persists across disable/re-enable, so turning it back on does not require rescanning the QR.

### `GET /api/users/me/totp/status`

Returns `{ "enabled": bool, "configured": bool }`. `configured` is `true` once the user has completed the setup flow at least once.

### `POST /api/users/me/totp/setup`

Generate a new shared secret and 10 recovery codes. Returns:

```json
{
  "secret": "BASE32SECRET",
  "otpauth_uri": "otpauth://totp/Sutr:you@example.com?secret=...&issuer=Sutr",
  "qr_data_url": "data:image/png;base64,...",
  "recovery_codes": ["xxxxx-xxxxx", "..."]
}
```

Recovery codes are only returned here — the server stores them as bcrypt hashes. Returns `409` if TOTP is already confirmed.

### `POST /api/users/me/totp/enable`

Verify a code from the authenticator and turn on 2FA.

**Body:** `{ "code": "123456" }`

**Response:** `{ "enabled": true, "configured": true }`. Returns `400` with an invalid code.

### `POST /api/users/me/totp/re-enable`

Re-enable TOTP after a prior disable. Only works if the user previously confirmed setup — otherwise returns `409`.

**Body:** `{ "code": "123456" }`

Requires a current authenticator code or an unused recovery code. Returns `403` if the code is missing or wrong.

### `POST /api/users/me/totp/disable`

Stop requiring codes on approvals. The secret and remaining recovery codes stay on file.

**Body:** `{ "code": "123456" }`

Requires a current authenticator code or an unused recovery code. Returns `403` if the code is missing or wrong.

---

## Logs

### `GET /api/logs`

Query tool call logs for your org.

**Query params:**
- `integration` — filter by integration ID
- `tool` — filter by tool name
- `limit` — max results (default `50`, max `500`)
- `offset` — pagination offset (default `0`)

**Response:**
```json
[
  {
    "id": 1,
    "org_id": "6ba7b810-...",
    "timestamp": "2026-04-09T12:00:00",
    "integration_id": "posthog",
    "tool_name": "get_events",
    "args_json": "{}",
    "result_json": "...",
    "error": null,
    "duration_ms": 342,
    "outcome": "executed",
    "approval_request_id": null,
    "args_hash": null,
    "additional_info": null
  }
]
```

The `outcome` field can be: `executed`, `pending` (awaiting human approval), `approved` (decided, not yet executed), `denied`, or `error`. Rows written by older versions may carry `approval_required`, the legacy spelling of `pending`.

Reading logs requires a **user session**, not an API key: the rows contain every caller's tool arguments and results. Agents and SDKs should read `/api/usage/*` instead, which is metering data and is key-readable.

Tool execution is rate limited per organization (`TOOL_RATE_LIMIT_PER_MINUTE`, default 120/min). Exceeding it returns `429` with `Retry-After`; refusals are logged nowhere and metered nowhere, because no upstream work happened.

`additional_info` carries the agent's optional explanation for the call (if any was supplied).

---

## OAuth 2.0 Authorization Server

MCP clients can authenticate to `/mcp` using the OAuth 2.0 Authorization Code + PKCE flow. The following endpoints are provided by the MCP SDK and Sutr's OAuth provider.

### Discovery

#### `GET /.well-known/oauth-authorization-server`

Returns RFC 8414 OAuth Authorization Server Metadata. No auth required.

#### `GET /.well-known/oauth-protected-resource/mcp`

Returns RFC 9728 Protected Resource Metadata for the `/mcp` endpoint. No auth required.

### Dynamic Client Registration

#### `POST /register`

Register an OAuth client (RFC 7591 Dynamic Client Registration). No auth required.

**Body:** `OAuthClientMetadata` JSON (redirect_uris, client_name, etc.)

**Response `200`:** `OAuthClientInformationFull` with generated `client_id` and `client_secret`.

### Authorization

#### `GET /authorize` or `POST /authorize`

Start the authorization flow. Redirects to the UI at `/oauth/authorize?session=<token>` for user approval.

**Query params:** `client_id`, `redirect_uri`, `response_type=code`, `code_challenge`, `code_challenge_method=S256`, `scope`, `state`, `resource`

### Token

#### `POST /token`

Exchange an authorization code or refresh token for access/refresh tokens.

**Form fields (authorization_code grant):** `grant_type=authorization_code`, `code`, `redirect_uri`, `code_verifier`, `client_id`, `client_secret`

**Form fields (refresh_token grant):** `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret`, `scope`

**Response `200`:**
```json
{
  "access_token": "eyJhbGci...",
  "token_type": "Bearer",
  "expires_in": 604800,
  "refresh_token": "eyJhbGci...",
  "scope": "..."
}
```

### Revocation

#### `POST /revoke`

Revoke an access or refresh token. Requires client authentication.

**Form fields:** `token`, `client_id`, `client_secret`

### OAuth UI Backend

These endpoints support the frontend OAuth authorization screen.

#### `GET /api/oauth/authorize/session?session=<token>`

Returns metadata about a pending authorization session for the UI to display. No auth required.

**Response `200`:**
```json
{
  "client_id": "...",
  "client_name": "My MCP Client",
  "redirect_uri": "http://localhost:3000/callback",
  "scope": "...",
  "resource": "...",
  "expires_at": 1234567890
}
```

#### `POST /api/oauth/authorize/approve`

Approve the authorization request. Requires user authentication.

**Body:**
```json
{ "session_token": "..." }
```

**Response `200`:**
```json
{ "redirect_url": "http://localhost:3000/callback?code=...&state=..." }
```

#### `POST /api/oauth/authorize/deny`

Deny the authorization request. No auth required.

**Body:**
```json
{ "session_token": "..." }
```

**Response `200`:**
```json
{ "redirect_url": "http://localhost:3000/callback?error=access_denied&state=..." }
```

---

## Health

### `GET /health`

```json
{ "status": "ok" }
```
