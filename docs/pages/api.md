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

### Update, rollback, and history

A deployment is versioned. An update is a **new revision of the same deployment** — it keeps the
deployment's id, its URL, and its history — never a delete followed by a create.

- `POST /api/deployments/{id}/update` — recompile the source OpenAPI project and apply the result
  as the next revision. Takes an optional `compile` block, the same shape the compile endpoint
  accepts. Returns the deployment plus `pending_revision`.
- `POST /api/deployments/{id}/rollback` — re-apply a retained revision. `{"revision": N}` names one;
  omitting it restores the most recent revision that actually ran. The **stored package is re-run,
  not rebuilt**, so a rollback restores what actually ran rather than what the source produces
  today. The restore is itself recorded as a new revision, so history stays append-only.
- `GET /api/deployments/{id}/revisions` — every version, newest first, with its origin
  (`create` / `update` / `rollback`), outcome (`pending` / `active` / `superseded` / `failed`),
  package SHA-256, tool count and URL.
- `GET /api/deployments/{id}/metrics` — runtime measurements, as far as the provider reports them.

Each revision builds a distinct, retained artifact (the image tag carries the revision number),
which is what makes a rollback a re-run rather than a rebuild. The local Docker provider keeps its
published port across an update, so the deployment's URL survives.

A provider that genuinely cannot update in place reports `supports_update = false` and the endpoint
refuses with a 400 rather than quietly recreating the deployment.

### Metrics: what each provider actually reports

Every field is optional, and a field a provider does not expose comes back as `null` with an
`unavailable_reason` — **never as `0`**, which would read as a measurement rather than an absence.

| Provider | Reports | Does not report |
| --- | --- | --- |
| `docker` | CPU %, memory used and limit, health, replicas | request counts, latencies — Docker does not see them |
| `gcp` | readiness, configured min instances | CPU/memory/requests/latency need the Cloud Monitoring API, a separate scope |
| `azure` | readiness, configured min replicas | the same numbers need Azure Monitor, a separate scope |
| `aws` | readiness, configured memory | the same numbers need CloudWatch `GetMetricData`, which the deploy role does not grant |
| `swaraj` | nothing | the provider is not implemented — see below |

### Swaraj Cloud

`swaraj` appears in `GET /api/deployments/providers` as **disabled**, with the reason
`NOT_CONFIGURED / DOCUMENTATION_REQUIRED`. The provider implements the full interface and none of
its behaviour: no part of the Swaraj Cloud API has been guessed. Implementing it needs official
documentation for the authentication model, base URL, workload endpoints, request/response schemas,
region model, image delivery, and secret injection.

Every provider operation resolves credentials fresh: an OAuth access token refreshed if due, or —
for AWS — the Identity Center token exchanged for short-lived role credentials.

CLI equivalents: `sutr deploy providers|list|create|status|logs|stop|start|delete`.
`sutr deploy create --provider gcp --connection <id> --config project=acme --config region=us-central1`.

---

## The `/v1` surface

Two API surfaces, on purpose.

`/api/...` is **frozen**. It is what the published `sutr-cli` and both SDKs read, and its bare-JSON
bodies are a compatibility promise rather than a style choice. Nothing on it will grow an envelope.

`/v1/...` is where the ESDS API standards apply: a response envelope, cursor pagination,
`X-Request-ID` / `X-Correlation-ID` / `Idempotency-Key`, and one error shape. Both surfaces call the
same service layer — a `/v1` route is never a second implementation.

### Response envelope

```json
{ "data": <the payload>, "meta": { "request_id": "...", "correlation_id": "...", "next_cursor": "..." } }
```

`data` is always present, even when null. `meta` is always an object, even when empty. Neither is
ever omitted: "absent" and "empty" being different is exactly the kind of subtlety that costs a
client an afternoon.

### Error shape

```json
{
  "error": {
    "code": "invalid_request",
    "message": "The `cursor` parameter is not a cursor this API issued.",
    "details": { "parameter": "cursor" },
    "retry_after": 30
  },
  "meta": { "request_id": "...", "correlation_id": "..." }
}
```

`code` is stable and machine-readable. `message` is for a human and **may be reworded at any time** —
branch on `code`. `details` and `retry_after` appear only when they apply.

| `code` | HTTP |
| --- | --- |
| `bad_request`, `invalid_request` | 400 |
| `unauthorized` | 401 |
| `forbidden` | 403 |
| `not_found` | 404 |
| `conflict` | 409 |
| `validation_failed` | 422 |
| `rate_limited`, `quota_exceeded` | 429 |
| `internal_error` | 500 |
| `unavailable` | 503 |

### Cursor pagination

`?limit=50&cursor=<opaque>`. The response's `meta.next_cursor` is the next page; **its absence means
the last page**. Default limit 50, maximum 200.

The cursor is opaque and must be treated as a token you received and hand back — never constructed.
Its encoding is free to change, and anything parsed out of it becomes a compatibility obligation
nobody agreed to. A cursor this API did not issue returns 400.

Offset paging is not offered here because it is wrong for a growing table: rows inserted between two
requests shift every later page, so a client walking pages either sees a row twice or misses it.

### Correlation

Send `X-Correlation-ID` to join several requests into one logical operation. It is echoed on the
response, attached to every log line, embedded in every event the request produces, and queryable —
`GET /v1/events?correlation_id=...` returns the whole chain.

Without one, the request id serves as its own correlation id.

### Idempotency

Send `Idempotency-Key` on a create to make a retry safe. Currently honoured by
`POST /api/deployments`; more operations gain it as the services that need it are built (build
prompt §76: billing, payments, registration, uploads, settlement, token issuance, tool publication,
runtime deployment).

- Same key, same body, work finished → the **original response**, replayed.
- Same key, same body, still running → **409** with `retry_after`.
- Same key, **different** body → **409**. Replaying the first response would silently discard the
  second request, which is worse than an error naming the client's bug.
- No key → unchanged behaviour. Idempotency is opt-in per request.

Keys are scoped to `(organization, endpoint, key)` and retained for 24 hours. A failed attempt
releases its key immediately, so a transient failure does not lock it out for a day.

---

## Platform

### `GET /v1/platform/capabilities`

Which optional backends are live, and what is degraded without them. Every optional dependency
degrades rather than failing, so the honest answer to "is Kafka on?" should come from the platform
rather than from someone's memory of the deployment.

```json
{
  "data": {
    "capabilities": [
      { "name": "event_bus", "available": true, "reason": null, "detail": "Backend: in_process. ..." },
      { "name": "secrets_kms", "available": false, "reason": "SECRETS_BACKEND is 'db', so secret values are stored unencrypted in the database.", "detail": "..." }
    ],
    "degraded": ["secrets_kms", "tracing", "metrics"]
  },
  "meta": { "request_id": "...", "correlation_id": "..." }
}
```

A working capability carries `reason: null` — a reason attached to something that is fine reads as a
warning about a non-problem.

### `GET /v1/platform/services`

The service map and plane split this build was assembled from, including which service writes which
table. Useful when reading logs, or when planning a split.

---

## Events

Every control-plane stage emits an immutable fact. When something does not happen, the first
question is "did the event get out?" — this is where to look.

Events are written to a **transactional outbox** in the same transaction as the state change they
describe, so a fact and its announcement commit together or not at all. A relay then publishes them.
Delivery is at-least-once, and consumers are idempotent.

### `GET /v1/events`

Newest first, cursor-paginated. Filters: `event_type`, `state`
(`pending` | `published` | `failed` | `dead_lettered`), `correlation_id`.

Scoped to your organization; platform-wide events (those with no tenant) are also visible.

```json
{
  "data": [
    {
      "event_id": "…", "event_type": "tool.registered", "event_version": 1,
      "partition_key": "customapi_petstore", "correlation_id": "…",
      "tenant_id": "…", "resource_id": "customapi_petstore",
      "producer": "registry", "state": "published", "attempts": 0, "last_error": null,
      "payload": { "integration_id": "customapi_petstore", "tool_count": 12 },
      "created_at": "…", "published_at": "…"
    }
  ],
  "meta": { "next_cursor": "…" }
}
```

### `GET /v1/events/dead-letter`

Events the relay gave up on after five attempts. This is the manual-investigation queue.

### `POST /v1/events/{event_id}/retry`

Return a failed or dead-lettered event to the queue. Idempotent by nature: an event already pending
or published comes back unchanged.

### Event types

39 types, listed in `server/src/sutr/events/topics.py` with a JSON Schema each in
`contracts/events/`. Every event carries the same envelope — `event_id`, `event_type`,
`event_version`, `timestamp`, `correlation_id`, `tenant_id`, `resource_id`, `producer`, `payload` —
so a consumer can read one without knowing its type.

Ordering is per entity: `provider.*` by provider, `tool.*` by tool, `runtime.*` by runtime,
`invoice.*` by invoice, `policy.*` by policy.

Emitted today: `api.uploaded`, `translation.completed`, `mcp.generated`, `tool.registered`,
`runtime.deployed`, `runtime.updated`, `runtime.rolled_back`, `runtime.failed`, and
`usage.recorded` when `EMIT_USAGE_EVENTS` is on. The rest are declared — their names are a contract
from the moment the first one is published — and are emitted as the phases that own them land.

---

## Sources

Where a project's definition comes from, and whether it still matches. The
onboarding question is not "upload your OpenAPI" but *"where does your API
definition currently live?"* — so a project can have several sources, one of
which is authoritative.

### `GET /v1/sources/connectors`

Every source type, including the ones that are **declared but not built**. A
source type that is silently missing looks like an oversight; one listed as
unavailable with a reason is a decision.

| Connector | Watch | Discover | Notes |
| --- | --- | --- | --- |
| `paste` | no | no | Content pasted in. Nothing to re-fetch. |
| `upload` | no | no | A file, with its filename kept as provenance. |
| `url` | yes | no | Polled with `If-None-Match` / `If-Modified-Since`. |
| `swagger_ui` | yes | yes | Finds the specification behind a Swagger UI page. |
| `github` | yes | yes | Polled by commit SHA — one API call, no file transfer. |
| `swaggerhub` | yes | no | Polled by re-fetching; SwaggerHub has no conditional request. |
| `postman` | no | no | Collection v2.0/v2.1 → OpenAPI, with conversion notes. |

Returned under `planned` with a reason: `wsdl`, `api_gateway`, `generic_git`.

### `POST /v1/sources/validate`

Translate a document and report **every stage**. The same pipeline an import
runs, so what this says is what an import will do.

```json
{ "data": {
    "ok": true, "ir_hash": "…", "ir_version": 2, "total_duration_ms": 5,
    "stages": [
      { "name": "parse",     "status": "ok",      "duration_ms": 0, "detail": {"declared_version": "3.0.3"} },
      { "name": "convert",   "status": "skipped", "detail": {"reason": "The document is already OpenAPI 3.x."} },
      { "name": "validate",  "status": "ok",      "duration_ms": 3 },
      { "name": "lint",      "status": "ok",      "detail": {"counts": {"error": 0, "warning": 2, "info": 4}} },
      { "name": "resolve",   "status": "ok" },
      { "name": "normalize", "status": "ok",      "detail": {"operations": 12, "ir_hash": "…"} }
    ],
    "findings": [], "warnings": []
} }
```

A failure halts the chain and names the stage: `failed_stage`, `error_code`,
`error_message`. A document that fails validation never reaches `normalize`,
and `ir_hash` stays null — semantic errors halt before IR generation.

### `POST /v1/sources/discover`

What definitions live at a source, before committing to one. Returns candidates
for the connectors that support it (`github`, `swagger_ui`).

### `POST /v1/sources`

Attach a source to a project. The source is **contacted before it is stored**,
so a mistyped URL or a missing token is reported while you are still looking at
the form rather than an hour later in a log.

```json
{
  "project_id": "…",
  "connector": "github",
  "config": { "url": "https://github.com/acme/api", "path": "openapi.yaml" },
  "role": "primary",
  "token": "ghp_…",
  "watch_enabled": true,
  "watch_interval_seconds": 900,
  "apply_policy": "non_breaking"
}
```

`role` is `primary` (the definition the project is generated from) or
`comparison` (watched only to report disagreement, never applied).

The token is stored through the secrets backend and never returned; responses
carry `has_token` instead.

### Watching, and what may happen automatically

| `apply_policy` | When a watched source changes |
| --- | --- |
| `never` (default) | Recorded as drift. A human decides. |
| `non_breaking` | Applied — **unless** anything is BREAKING or SECURITY. |
| `always` | Applied whatever changed. |

A withheld change records **why** it was withheld, because "nothing happened"
has to be explicable.

Applying updates the project's **definition**. It does not recompile tools or
redeploy runtimes: each of those has its own gate, and a background loop should
not reach through them.

Watching backs off exponentially on failure (capped at 16×) and pauses after 20
consecutive failures, with the reason left on the source rather than the source
just going quiet.

### `POST /v1/sources/{id}/check`

Fetch now and report what changed.

```json
{ "data": { "status": "changed", "breaking": 1, "applied": false,
            "detail": "1 breaking change(s) were detected and the apply policy is 'non_breaking', so the change was not applied.",
            "drift_report_id": "…" } }
```

`status` is `unchanged`, `changed`, or `failed`. Three different things all
report `unchanged`, and the `detail` says which: the source answered 304, the
bytes are identical, or **the document changed but the API it describes did
not** — a reformat is not drift.

### `GET /v1/sources/{id}/drift`

Cursor-paginated drift reports, newest first. Filter with `?status=open`.

Every change is classified:

| Category | Meaning |
| --- | --- |
| `BREAKING` | Existing callers stop working: an operation removed, a parameter made required, a type changed, an `operationId` renamed (which renames the generated tool). |
| `NON_BREAKING` | Additive: a new operation, a newly optional parameter. |
| `SECURITY` | Any change to authentication, **in either direction**. Adding it breaks callers; removing it makes the API public. Both need a human. |
| `DOCUMENTATION` | Summaries, descriptions, documented responses. |
| `METADATA` | Version, title, tags. |

Comparison is over the **canonical IR**, not the document, so a reformatted
file, a reordered key map, or a migration from Swagger 2.0 to OpenAPI 3.x
produces no drift at all.

### `POST /v1/sources/drift/{id}/apply` · `/dismiss`

Where a human says yes or no. Both are audited. Applying a report that is
already resolved returns 409.

### `POST /v1/sources/{id}/compare/{other_id}`

Report where two attached sources disagree — the source of truth in a
repository versus what a gateway is actually serving. Produces a report that is
never applied; it exists to surface the disagreement.

---

## Documentation

What a specification cannot say. An OpenAPI document describes shapes; it does
not say that refunds are only allowed within 30 days, that a chargeback is a
reversal initiated by the cardholder's bank, or that the refund process is four
steps in a fixed order. Upload the prose, and get those back — each one
carrying the sentence it came from.

Every fact returned by these endpoints has a `citation`: the source document,
the section path, the page, and the text verbatim. That is the whole point of
the service. A rule you cannot trace is a rule you cannot act on.

### `POST /v1/documentation/jobs`

```json
{
  "provider_id": "8f14…",
  "document_uri": "https://provider.example.com/refund-policy.pdf",
  "document_type": "pdf"
}
```

All fields optional. `provider_id` is the OpenAPI project this documentation
describes — omit it for a policy or a glossary that belongs to no single API.
Instead of `document_uri` you may send `content` with the document inline.

A `document_uri` is fetched through the same SSRF screen as every other
outbound request, and the response is size-capped while streaming.

Returns `201` with the `job_id`, the job's per-stage record, and the processed
document. Processing runs inline, so this call takes as long as the document
takes — a large PDF is not a fast request. The job's stage record is real
either way, and a failed stage can be resumed rather than restarted.

### `POST /v1/documentation/documents`

The same thing as a multipart upload: `file`, plus optional `provider_id`,
`document_type` and `extractor` form fields. The format is detected from the
content, not from the filename — a `.txt` that begins `%PDF-1.4` is a PDF.

### `GET /v1/documentation/jobs/{id}`

The stage record: `parse`, `chunk`, `extract`, `graph`, `embed`, each with its
status, duration and what it produced, plus any `degradations`.

A document ends in one of three states, and the difference matters:

| Status | Meaning |
|---|---|
| `processed` | Every stage produced what it should. |
| `partial` | Processed, and something was lost. `degradations` names what and why — unreadable pages, a graph backend that threw, no embedding provider. |
| `failed` | Parsing failed. A document nobody can read has nothing downstream to do. |

`partial` is the common outcome on a default install, because no embedding
provider ships. That is deliberate: the state says so rather than reporting a
clean success over a document with no vectors.

### `GET /v1/documentation/rules`

Extracted business rules. Filter by `document_id`, `rule_type`
(`validation`, `eligibility`, `limits`, `compliance`, `approval`, `exceptions`,
`security`, `pricing`, `regional`, `role_based`) and `min_confidence`.

```json
{
  "rule_type": "limits",
  "condition": "only within 30 days of purchase",
  "action": "Refunds are allowed",
  "confidence": 0.8,
  "extractor_version": "rule-based-v1",
  "citation": {
    "document": "refund-policy.pdf",
    "location": "Refund Policy > Eligibility, page 2, chars 1180–1231",
    "text": "Refunds are allowed only within 30 days of purchase."
  }
}
```

### `GET /v1/documentation/workflows` · `/glossary`

Procedures as ordered nodes and edges; defined terms with their definitions and
aliases. Both filterable by `document_id`, both cited.

### `GET /v1/documentation/graph`

The knowledge graph. With `entity_type` and `entity_key`, the neighbourhood
around one node to a `depth` of 1–3; with `document_id`, everything one
document produced.

### `GET /v1/documentation/search`

Search this organisation's documentation. `q` is required; `document_id` and
`limit` are optional.

The response says which mode it ran in:

```json
{
  "mode": "lexical",
  "semantic": { "available": false, "unavailable_reason": "NOT_CONFIGURED: …" },
  "hits": [ … ]
}
```

`lexical` is BM25 over chunks. `hybrid` means an embedding provider is
configured and the two rankings were fused. The distinction is reported rather
than hidden, because a caller that believes it got semantic search will trust
the ranking differently.

### `GET /v1/documentation/capabilities`

What this deployment can actually do to a document: which formats it can parse,
whether OCR is available, which extractors exist, whether embeddings are
configured, and where originals are stored. Every unavailable capability comes
with a reason.

Worth calling before uploading a scanned PDF — it will tell you the result is
going to be empty, and why.

---

## MCP generation

A **runtime artifact** is one build of an MCP server: the package bytes, an MCP
manifest, a CycloneDX SBOM, a validation report, the documentation knowledge
that went into it, and — when a signing key is configured — a signature over
its build hash.

Artifacts are immutable. There is no route that edits one, and generating again
from unchanged inputs returns the artifact you already have rather than making a
second one. That is how the determinism claim stays checkable: a duplicate would
mean the build was not deterministic.

### `POST /v1/generation/runtimes`

```json
{
  "ir_uri": "sutr://openapi-projects/8f14…",
  "knowledge_uri": "sutr://openapi-projects/8f14…",
  "runtime": "python",
  "compile": { "filters": { "exclude_tags": ["admin"] } }
}
```

`ir_uri` names an OpenAPI project; a bare project id works too, and so does
`project_id`. External URIs are **refused, never fetched** — an endpoint that
dereferenced an arbitrary URI would be a request-forgery primitive.

`knowledge_uri` is optional and defaults to the same project. It names the
project whose extracted rules, glossary and workflows are folded into the build.

`runtime` accepts `python`. `go` and `node` are declared and refused by name:
the LLD names three templates and one is implemented.

`compile` takes the same filters, server choice and auth configuration as
`POST /api/openapi/{id}/package`.

Returns `201` with the stages the run went through, a knowledge summary, the
workflow steps that were linked to operations, and the artifact.

### `GET /v1/generation/runtimes`

Every artifact for the caller's org, newest first. Filter with `project_id`.

### `GET /v1/generation/runtimes/{id}`

The artifact with its manifest, its validation report, the knowledge it carries,
and a `verification` block: whether the stored bytes still hash to the recorded
digest, and whether the signature checks out.

### `GET /v1/generation/runtimes/{id}/package`

The exact zip that was validated, with `X-Sutr-Package-Sha256` and
`X-Sutr-Build-Hash` headers.

### `GET /v1/generation/runtimes/{id}/sbom`

The CycloneDX 1.5 document, served as `application/vnd.cyclonedx+json` rather
than wrapped in an envelope, so it can be piped straight into a tool that
expects one.

Dependency entries carry the **declared constraint**, not a resolved version:
the package pins ranges, there is no lock file, and the index is never
contacted. An empty `version` with the constraint in a property is the honest
shape; a plausible-looking number would not be.

### `GET /v1/generation/runtimes/{id}/manifest` · `/validation`

The manifest and the validation report on their own.

Each validation check reports `ok`, `failed`, `blocked` or `skipped`.
**`blocked` is not a pass** — it means the check could not run on this install,
and it is named in the report and in the summary sentence. A default install
has `security_scan` and `vulnerability_scan` blocked, because no scanner ships
and none is invented.

### `GET /v1/generation/capabilities`

Which runtimes this build can generate, which checks it can actually run, and
whether artifacts are being signed. Worth reading before trusting a `validated`
status: what that status covers depends on what the operator configured.

### Deploying an artifact

`POST /api/deployments` takes `artifact_id` instead of `project_id`. Only a
validated artifact is accepted; a rejected one is refused with `409` and the
name of the check that objected. The bytes deployed are the bytes that passed
the gate, and the credential is injected under the variable the artifact's
manifest declares — not one derived from the deployment's name.

`POST /api/deployments/{id}/update` accepts `artifact_id` the same way, for
promoting a build you have already tested.

### `GET /api/deployments/{id}/drift`

What the provider actually has, against what the platform declared: the live
revision, the package digest, and whether it is up. Detection only — nothing is
corrected as a side effect of looking. `checked: false` with a reason is a
distinct answer from a clean report.

## Governance

Versioned policy, compliance, risk, the approval workflow, and exceptions.
Everything is scoped to the caller's organization.

### `POST /v1/governance/policies`

```json
{
  "key": "refund-controls",
  "name": "Refund controls",
  "kind": "access",
  "document": { "rules": [
    { "name": "no-refunds-outside-finance", "effect": "deny",
      "subject":  { "role": "support" },
      "resource": { "integration_id": "payments", "tool_name": "refund_payment" } }
  ]}
}
```

Creates the policy and its first **draft**. A policy is the identity; every
statement lives in a version, and a version stops being editable the moment it
leaves DRAFT.

**Writing a policy is not deploying it.** Only an `ACTIVE` version's rules reach
the decision point, so a draft changes no live decision.

### `POST /v1/governance/policies/{id}/versions/{n}/transition`

    Draft → Review → Approved → Published → Active → Deprecated → Archived

Review can send a version back to Draft; everything else moves forward or
retires. `PUBLISHED` and `ACTIVE` are separate because a version can be released
without being the one enforced, and exactly one is active at a time.

**The author of a version may not approve it.** Unconditionally — unlike the
registry's change gate, which exempts organizations with a single administrator.
A tenant with no policies loses nothing, so the rule can be absolute.

### `POST /v1/governance/policies/{id}/rollback`

Restores the previously active version. The fail-safe for a deployment that went
wrong; it is one pointer move, and it does not re-validate the restored version,
which was active before.

### `POST /v1/governance/evaluate`

```json
{ "integration_id": "payments", "tool_name": "refund_payment", "as_agent_id": "…" }
```

What the decision point concludes, and why — **changing nothing**. Every rule
that matched reports its `source`: a standalone access rule, or a policy with
its key and version.

### `POST /v1/governance/compliance/runs`

```json
{ "framework": "soc_2" }
```

**This does not certify anything.** Of the eighteen controls in the catalogue,
twelve are checked against facts the platform actually holds and six are
reported `manual` with the reason — training, physical security, vendor
management and the like happen outside software. A `manual` result is never
counted as a pass, and every response carries the scope in words.

A default install fails two controls by design: plaintext secrets, and no
configured security scanner. That is the point of the controls.

`simulate_timeout` exercises the fail-safe path: the run is recorded
`timed_out`, and the approval workflow blocks publication on it.

### `GET /v1/governance/risk/{tool_id}`

A risk score per tool where **lower is better** — the opposite direction from
the registry's trust score, and nothing converts between them. Seven components,
each reporting its evidence, weights summing to 100.

An unmeasurable component **abstains** rather than counting as no risk, and
`coverage` says how much was measured. Two scores are only comparable at similar
coverage: a component becoming measurable changes the denominator.

### `POST /v1/governance/reviews`

    Upload → Automated Validation → Security Scan → Compliance Review →
    Manual Approval → Publication

Every stage's outcome is **sourced** — the registry versions, the generation
validation report, that report's scan checks, a real compliance run — and each
result names where it came from. A stage that cannot be evidenced blocks, and a
blocked review cannot be decided.

`POST .../decide` records the manual approval; whoever opened the review may not
decide it. `POST .../auto-approve` applies §5.2.8's low-risk path, and requires a
score **below** the threshold: a null score never auto-approves, because unknown
risk is not low risk.

### `POST /v1/governance/exceptions`

```json
{ "violation": "secrets are stored in plaintext",
  "justification": "Vault lands next sprint; this blocks the pilot.",
  "control": "crypto.secrets_at_rest", "days": 14 }
```

**All exceptions expire.** There is no "never", and the maximum is 90 days.
Approval requires a risk assessment first (`POST .../assess`) — the LLD's flow
puts assessment before decision, and one that skipped it cannot be reviewed
later. The requester may not decide their own request.

Revalidation falls due at half the term, because a 90-day exemption reviewed on
day 89 was not reviewed.

### `GET /v1/governance/capabilities`

Which of the seven governance points are actually gated and by which module,
the four fail-safe behaviours and where each is implemented, and what each
engine can honestly do.

## Provisioning

Identity, authorization, and the scoped access passes issued after a decision.
Everything is scoped to the caller's organization; another tenant's agent, rule
or pass reads as `404`.

### `GET /v1/provisioning/whoami`

The principal this request is acting as: `sutr:user:…`, `sutr:agent:…`, or —
when an API key has no identity bound to it — `sutr:api-key:…`. An unbound key
is an actor with no identity, and it is named as one rather than dressed as an
agent.

### `POST /v1/provisioning/agents`

```json
{
  "name": "finance-agent",
  "description": "Issues refunds on behalf of the finance team.",
  "attributes": { "role": "finance", "region": "India" },
  "api_key_id": "…"
}
```

An identity is separate from the credential that authenticates it. Binding a key
makes calls with that key act as this agent, with these attributes; the identity
outlives key rotation. One key binds to at most one identity.

Attributes are **your** claims about **your** agent. Nothing verifies them, and
every response carrying them says `attributes_are_tenant_declared`.

`POST /v1/provisioning/agents/{id}/revoke` retires the identity **and, by
default, every live pass it holds** — which is the right default for the
situation the endpoint exists for.

### `POST /v1/provisioning/rules`

The LLD's own example, as a rule:

```json
{
  "name": "finance-india-refunds",
  "effect": "allow",
  "subject":  { "role": "finance", "region": "India" },
  "resource": { "integration_id": "payments", "tool_name": "refund_payment" }
}
```

Every matcher present must match; an absent one matches anything. **Deny wins**
whatever the priority — priority only orders which rule is reported. A rule set
with no matching rule has **no opinion**, so an organization with no rules keeps
the behaviour it had.

Two things are refused at write time rather than becoming rules that never fire:
a rule with no matchers at all, and a misspelled resource attribute.

### `POST /v1/provisioning/decisions`

```json
{ "integration_id": "payments", "tool_name": "refund_payment", "as_agent_id": "…" }
```

What the decision point concludes, and why — **changing nothing**. Every layer
reports its effect (`allow`, `deny` or `abstain`) and a reason:

```json
{ "layer": "rbac", "effect": "abstain",
  "reason": "This principal carries no role — API keys and services are not role-checked. Use an access rule to constrain it." }
```

Layers that did not run are absent, not recorded as agreeing. Layer 4 — the
per-tool execution policy — is evaluated by the enforcement point immediately
after, and the response says so.

### `POST /v1/provisioning/passes`

```json
{
  "tools": ["refund_payment"],
  "integration_id": "payments",
  "purpose": "issue one refund",
  "ttl_seconds": 300,
  "as_agent_id": "…"
}
```

A short-lived, single-purpose, least-privilege, signed, revocable JWT — the
shape every server Sutr generates already validates under
`GOVERNANCE_MODE=platform`.

The token is returned **once** and is not stored: what the platform keeps is the
claims and the decision that granted them. Re-reading the pass never returns the
token again.

Asking for tools you may not use **narrows** the pass rather than refusing it,
and the response says which tools were dropped and why. A request where nothing
is permitted is refused with the rule that refused it.

`POST /v1/provisioning/passes/{id}/revoke` revokes one. Revocation is enforced
where the platform verifies the pass; a generated MCP server validates offline —
which is what lets it keep serving when the control plane is down — and cannot
see it. The short lifetime is what bounds that gap, and the response says so.

### `GET /v1/provisioning/capabilities`

The four layers and which are decided where, the pass rules and TTLs, and where
secrets actually live — including whether they are encrypted at rest and whether
anything mints temporary credentials (nothing does).

## Discovery

Natural-language intent in, a ranked and policy-filtered list of registry tools
out. Read-only: nothing here writes a table, and nothing calls a provider API.

### `POST /v1/discovery/search`

```json
{
  "intent": "refund a customer payment",
  "limit": 10,
  "region": "eu-west-1",
  "compliance": ["SOC2"],
  "entitled_only": false,
  "include_deprecated": false,
  "ranking_version": "balanced-1"
}
```

Every result carries its own arithmetic:

```json
{
  "tool_key": "refunds-api",
  "score": 1.438,
  "matched_terms": ["payment", "refund"],
  "contributions": { "relevance": 1.0, "trust": 0.234, "entitlement": 0.15 },
  "explanation": "1.438 = relevance +1.000 trust +0.234 entitlement +0.150 …"
}
```

The response also carries, always:

- **`policy`** — the filter version, which checks ran, and how many candidates
  each excluded. The filter runs **before** ranking, and `stages` shows it.
- **`degradations`** — what was skipped and what that did to the answer. An
  empty list is a claim that nothing was skipped.
- **`retrieval.mode`** — `keyword`, `vector`, `graph`, `hybrid` or `none`, named
  by what actually contributed an ordering.
- **`stale_results`** — listings served from an index that is behind the
  registry. They are returned and flagged, not withheld.
- **`cached`** — whether this came from the query cache.

**When nothing matches**, `results` is empty and `suggestions` is not: the tools
that matched your intent, each with the policy check that excluded it and why
(*"You are not subscribed to this tool"*, *"The provider declares this tool for
us-east-1, not eu-west-1"*). An agent that gets an empty list learns nothing; one
that gets the reason can act.

### `GET /v1/discovery/capabilities`

Which rankers this install actually has, which policy checks have anything to
read, the ranking versions, and what the cache is. Worth reading before trusting
a ranking: on a default install the vector ranker is unavailable, two of the
eight policy checks report `enforced: false`, and the cache is per replica.

### `POST /v1/discovery/evaluate`

```json
{
  "judgements": [
    { "intent": "refund a payment", "relevant_tool_ids": ["…"] }
  ],
  "k": 10
}
```

Precision, recall and NDCG for judgements **you** supply. No relevance
judgements ship with the platform, so there is nothing to score against until
you say what a good answer is — and the response says that plainly. Each
judgement runs a real, uncached query; measuring a cached answer would be
measuring the cache.

Recall comes back `null` when a judgement lists nothing relevant: "found
everything" and "there was nothing to find" are different results.

### `POST /v1/discovery/cache/invalidate`

Drops this tenant's cached answers. Registry events already invalidate the
cache; this is for what they cannot see — a trust score that moved because a
deployment failed or calls started erroring, neither of which the registry
announces.

## Registry

The authoritative record for a tenant's tools: metadata, immutable versions,
governance state, pricing and an explainable trust score. Everything is scoped
to the caller's organization; another tenant's tool reads as `404`.

### `POST /v1/registry/tools`

```json
{
  "tool_key": "refunds-api",
  "name": "Refunds API",
  "summary": "Issue and track refunds.",
  "category": "Finance",
  "tags": ["payments"],
  "regions": ["eu-west-1"],
  "compliance": ["SOC2"],
  "project_id": "8f14…",
  "integration_id": "customapi_refunds"
}
```

`tool_key` is an address: lowercase, 3–64 characters, and never renamed. Tools
start `DRAFT` and `private`.

`regions` and `compliance` are the provider's own claims. Every response that
carries them also carries `declared_by_provider` saying so — nothing in this
platform has verified them.

### `GET /v1/registry/lifecycle`

The whole state machine: fourteen pipeline states, six failure branches with the
stage each one resumes at, every allowed transition, and which two need
approval. Read it rather than hard-coding it.

### `POST /v1/registry/tools/{id}/transition`

```json
{ "to": "UNDER_REVIEW", "reason": "ready" }
```

An ungated transition applies immediately (`applied: true`). A gated one —
`DEPLOYED → UNDER_REVIEW` and `APPROVED → PUBLISHED` — returns
`applied: false` with a `change_request_id`, and **changes nothing**.

A transition the machine does not allow is refused with both ends named and the
possible alternatives listed.

### `POST /v1/registry/tools/{id}/versions`

Cuts the next immutable version, normally from a validated runtime artifact
(`artifact_id`). The version copies the artifact's build hash, SBOM and
deployment manifest onto itself, so it stays complete and rollback-able on its
own. An artifact that failed validation cannot become a version.

### `POST /v1/registry/tools/{id}/visibility` · `/pricing`

Both are gated: they return a change request and apply nothing. A price is
validated at request time, so an approver never discovers it was malformed.
`GET .../pricing` returns the live price and every price the tool has had.

### `POST /v1/registry/change-requests/{id}/decide`

```json
{ "approve": true, "note": "looks good" }
```

Approving applies the change in the same transaction. A requester may not decide
their own change **when the organization has another eligible approver**; a solo
install may, and the record says `self_decided: true`.

### `GET /v1/registry/tools/{id}/trust`

The score, and why it is what it is:

```json
{
  "score": 42, "max": 100, "coverage": 0.2,
  "components": [ { "name": "governance_status", "weight": 10, "value": 0.5,
                    "points": 5, "available": true, "detail": {…} }, … ],
  "explanation": "42/100 from governance_status 5/10, doc_quality 3/10. Not measured: security_scans, validation_success, runtime_availability, error_rate, user_ratings."
}
```

An input that cannot be measured is **excluded**, not scored as zero, and
`coverage` says how much of the total weight was measured. With nothing
measurable the score is `null` with a reason — a tool nothing is known about is
not a tool known to be bad.

### `POST /v1/registry/tools/{id}/deprecate` · `/archive`

Deprecation needs a note saying what to use instead. A deprecated tool keeps its
subscribers and stops taking new ones; archiving removes it from the storefront.

## Marketplace (registry storefront)

`/v1/marketplace` is the cross-tenant storefront. It is a **projection** of
registry events, so it can lag: every listing carries `projected_at` and
`projected_from`, and nothing authorizes off a listing — subscribing re-reads the
registry.

(The older `/api/marketplace` is a different surface: the console's catalog of
bundled integrations and APIs compiled in this org. It now fills its trust,
pricing and version fields from a registry record where one names the same
`integration_id`.)

### `GET /v1/marketplace/listings`

Filter with `q`, `category`, `tag`, `min_trust`, `provider_org_id`; sort by
`trust` (default), `subscribers`, `recent` or `name`. Only published listings
are returned, and only ones that are public or belong to the caller's own
organization.

An unscored listing sorts *below* scored ones rather than above them by virtue
of `null`, and `min_trust` excludes it rather than treating it as zero.

### `GET /v1/marketplace/listings/{tool_id}`

The listing with its versions, the full trust explanation, and the caller's own
subscription if they have one.

### `GET /v1/marketplace/providers/{org_id}` · `GET/PUT /v1/marketplace/profile`

A provider's profile and their published listings. `verified` is absent from the
PUT body by design: a badge a provider can set is a badge that means nothing, so
it is platform-granted.

### `POST /v1/marketplace/subscriptions`

Discover → **Subscribe** → Provision → Use → Renew → Cancel.

```json
{ "tool_id": "…" }
```

Returns `state: "subscribed"` and `entitled: false`. `POST .../provision` moves
it to `active` and entitled (or to `failed` with a reason, if you pass one).
`POST .../renew` extends the term without losing days already paid for, and
`POST .../cancel` ends it — the row is kept, so "who had access in March" stays
answerable.

The price is snapshotted at subscribe time, so a later price change does not
reprice an existing subscriber. **Nothing here is billed**:
`billed_by_this_platform` is false on every price and subscription.

`GET /v1/marketplace/subscriptions` is what you subscribed to;
`GET /v1/marketplace/subscribers` is who subscribed to your tools.

## Marketplace

The tool catalogue as a storefront: categorised, counted, rated, filterable. Separate from
`/api/integrations`, which is the raw catalogue and keeps its shape.

### `GET /api/marketplace/listings`

Query parameters, all optional: `q` (matches name, description, and tags — terms are ANDed),
`category`, `tag` (repeatable; **all** given tags must match), `type` (`remote_mcp` | `custom`),
`installed`, `available`, `sort` (`popular` | `rating` | `name`), `limit`, `offset`.

```json
{
  "total": 59,
  "limit": 24,
  "offset": 0,
  "sort": "popular",
  "listings": [
    {
      "integration_id": "stripe",
      "name": "Stripe",
      "category": "Finance",
      "tags": ["payments", "billing"],
      "tool_count": null,
      "install_count": 3,
      "installed": true,
      "rating": 4.5,
      "review_count": 2,
      "trust_score": null,
      "compliance": [],
      "regions": [],
      "pricing": null,
      "versions": [],
      "pending_fields": ["trust_score", "compliance", "regions", "pricing", "versions"],
      "pending_reason": "Not available yet: this field is owned by the Registry service, which is not implemented."
    }
  ]
}
```

Two nulls in that response mean different things, and neither means zero:

- `tool_count: null` — this is a remote MCP server whose tool list lives upstream and is only known
  after a discovery call.
- `trust_score: null` and the other `pending_fields` — these belong to the Registry service, which
  is not implemented. A trust score of `0` would say the tool is untrustworthy; `null` says the
  platform does not know.

### Other endpoints

- `GET /api/marketplace/categories` — every category with its listing count.
- `GET /api/marketplace/tags?limit=40` — the most common tags, most frequent first.
- `GET /api/marketplace/listings/{integration_id}` — one listing.
- `GET /api/marketplace/listings/{id}/reviews` — reviews written **inside your organization**, with
  the mean rating and a 1–5 distribution. Nothing is published anywhere.
- `PUT /api/marketplace/listings/{id}/reviews` — `{"rating": 1..5, "title"?, "body"?}`. One person
  holds one opinion per integration: re-submitting **replaces** the previous rating rather than
  stacking a second vote, so an aggregate cannot be inflated by resubmission.
- `DELETE /api/marketplace/listings/{id}/reviews/{review_id}` — the author, or an org admin.

CLI: `sutr marketplace search|show|categories|rate`.

---

## Quotas

Usage limits, evaluated **before** a tool executes — never after, and never after the provider has
already been called.

### `GET /api/quotas`

Each configured limit with its current consumption. Counts are derived from the usage ledger rather
than a separate counter, so what a quota reports and what you are billed for cannot disagree.

### `PUT /api/quotas`

```json
{ "kind": "daily_tool_calls", "limit_value": 10000, "scope": "tenant", "scope_id": "", "enabled": true }
```

Idempotent on `(kind, scope, scope_id)`.

| `kind` | Window | Enforced |
| --- | --- | --- |
| `daily_tool_calls` / `monthly_tool_calls` | day / calendar month | yes |
| `concurrent_tool_calls` | instantaneous | yes, **per process** |
| `daily_tokens` / `monthly_tokens` | day / month | **no** — see below |
| `daily_data_transfer_bytes` / `monthly_data_transfer_bytes` | day / month | **no** — see below |

`scope` is `tenant` (everything the org does), `integration` (`scope_id` = the integration id), or
`tool` (`scope_id` = `<integration_id>/<tool_name>`). A limit of `0` is treated as unset, not as
"block everything".

Token and data-transfer quotas can be stored, but **nothing records those numbers per call yet**, so
they are not enforced. The listing marks them `enforceable: false` with a note, and the server logs
a warning, rather than silently passing or silently blocking.

`concurrent_tool_calls` is counted in process. With several workers each holds its own view, so the
effective limit is `limit × workers`. A shared counter is the fix and is not implemented.

### What a breach looks like

REST returns **429** with a `Retry-After` header counting down to the window rollover:

```json
{
  "error": "quota_exceeded",
  "message": "Quota 'daily_tool_calls' exceeded for this tenant: 10000 of 10000 used.",
  "quota": "daily_tool_calls",
  "scope": "tenant",
  "limit": 10000,
  "used": 10000,
  "retry_after": 18509,
  "integration_id": "stripe",
  "tool_name": "create_charge"
}
```

Over MCP the agent gets the same information as prose, with an explicit instruction not to retry in
a loop.

A refused call is **logged** (outcome `quota_exceeded`) and **metered at quantity 0**, so the
refusal is visible and auditable without being billed as an execution.

CLI: `sutr quota list|set|remove`.

---

## OpenAPI linting

Every imported specification is linted. Linting is **advisory**: structural validity against the
official meta-schema is what blocks an import; lint findings predict how good the generated tools
will be.

- `GET /api/openapi/lint/rules` — every rule, with its id, severity, summary and applicable
  OpenAPI versions.
- `POST /api/openapi/lint` — `{"content": "<the document>"}`. Lints without storing anything.

Findings are returned **in full** — all of them, every time. Each carries `rule_id`, `severity`
(`ERROR` | `WARNING` | `INFO`), `message`, `location`, an RFC 6901 `json_pointer` into your original
document, a `documentation` anchor, and `remediation`.

Rule ids follow [Spectral's](https://github.com/stoplightio/spectral) `oas` ruleset where the same
check exists; rules with no Spectral equivalent carry a `sutr-` prefix. Structural violations are
reported under `oas3-schema` — **every** violation, not just the first.

`POST /api/openapi/import` failures now carry `findings` and `finding_counts` alongside `message`,
and `GET /api/openapi/{project_id}` carries `lint_findings` and `lint_summary`.

The full rule reference is at [OpenAPI lint rules](/openapi-linting).

---

## Per-scheme credentials

A compiled API can declare several security schemes, and they are not interchangeable: an API key
belongs in one specific header, query parameter, or cookie, and an OAuth2 client-credentials grant
needs a client id and secret rather than a token.

- `GET /api/integrations/{integration_id}/credentials` — every scheme the specification declared,
  what each one needs (`secret` | `basic` | `client_credentials` | `authorization_code`), where it
  goes on the wire, and whether it is configured. **No credential value is ever returned.**
- `PUT /api/integrations/{integration_id}/credentials` — configure one scheme.
  `{"scheme_name": "...", "value": "..."}` for an API key or bearer;
  `{"scheme_name": "...", "username": "...", "password": "..."}` for basic (encoded server-side);
  `{"scheme_name": "...", "client_id": "...", "client_secret": "..."}` for client credentials.
- `DELETE /api/integrations/{integration_id}/credentials/{scheme_name}` — remove one.

For an OAuth2 **client-credentials** scheme the platform runs the grant itself: it calls the token
endpoint (SSRF-screened), caches the access token, and refreshes it before expiry. It does not ask
you to paste a token you obtained by hand.

Schemes the runtime cannot use — mutual TLS, implicit and password grants, exotic HTTP schemes —
are reported as unusable **with the reason**, and cannot hold a credential. They are never silently
downgraded to "paste a bearer token".

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

## Billing, metering and settlement (`/v1`)

The ESDS surface from LLD §5.1, alongside (not instead of) the `/api/usage` and
`/api/billing` routes above. Every route is org-scoped: another tenant's
invoice, ledger, plan or settlement reads as `404`, never as an empty success.

**Prices are evaluated at billing time, never during execution.** A metered
event is a fact about what happened; nothing on the execution path can reach a
pricing plan. Repricing a tool therefore does not reprice yesterday's usage.

**Nothing here collects or pays out money.** Invoices are computed and
settlements calculated; moving money needs a payment processor this platform
wires only for its own subscription plan. Every response says so in a field
rather than leaving a reader to assume.

Money is **integer micro-units** (`1000000` = one unit of currency) and rates are
**basis points** (`250` = 2.5%). No amount is ever a float.

### `POST /v1/metering/events`

```json
{ "kind": "tool_call", "invocation_id": "inv-8f3a", "integration_id": "payments",
  "tool_name": "refund_payment", "duration_ms": 284, "tokens": 1200, "region": "in-west-1" }
```

```json
{ "id": "…", "quantity": 1, "deduplicated": false, "priced": false,
  "pricing_note": "Prices are evaluated at billing time, never during execution." }
```

There is deliberately **no amount field**. Supply an `invocation_id` and a replay
is a no-op: the original event is returned with `deduplicated: true`. Dedupe is
scoped to your organization, so two tenants may use the same id.

### `GET /v1/metering/capabilities`

What this install can price, settle and collect — and what it cannot. Reports
the implemented pricing models, whether the ledger is append-only, the default
revenue share and whether it is hard-coded, and `collects_payment: false`.

### `POST /v1/billing/plans` · `GET /v1/billing/plans` · `POST /v1/billing/plans/{id}/publish`

```json
{ "key": "standard", "model": "tiered",
  "tiers": [{"up_to": 100, "amount_micros": 500000}, {"up_to": null, "amount_micros": 200000}],
  "discount_bps": 500, "tax_bps": 1800, "tax_label": "GST", "publish": true }
```

Eight models: `free`, `per_invocation`, `per_api_call`, `per_second`,
`subscription`, `tiered`, `hybrid`, `enterprise`. Tiering is **graduated** —
each band is charged at its own rate, so one extra call does not reprice the
whole quantity.

A plan is validated at **publish** time, where a mistake is cheap, and is
immutable once published. Changing a price creates a new version and supersedes
the old one, so an invoice can name the exact version each line used.

### `POST /v1/billing/invoices`

```json
{ "period_start": "2026-08-01T00:00:00Z", "period_end": "2026-09-01T00:00:00Z", "issue": true }
```

Prices the period's usage. Every line stores its arithmetic:

```json
{ "number": "INV-202608-1fa5a9a2", "state": "issued",
  "subtotal_micros": 80000000, "discount_micros": 4000000,
  "tax_micros": 13680000, "total_micros": 89680000,
  "plan_versions": [{"key": "standard", "version": 1, "model": "tiered"}],
  "lines": [{ "quantity": 250, "breakdown": { "steps": [
      "usage: 250 unit(s) of tool_call", "plan: standard v1 (tiered) → 80 USD",
      "discount: 5% → −4 USD", "GST: 18% → +13.68 USD", "charge: 89.68 USD" ]}}],
  "unpriced": [],
  "collected_by_this_platform": false }
```

`unpriced` names usage for which no plan was published — reported rather than
billed at zero or silently dropped, since both would be decisions the platform
is not entitled to make for you.

Generation never touches metering. A failure records `state: "failed"` with a
reason and an attempt count; generating the same period again **retries the same
row** rather than creating a second invoice. Pass `simulate_failure` to exercise
that path. Nothing schedules the retry — an operator or a worker calls it.

### `GET /v1/billing/invoices` · `GET /v1/billing/invoices/{id}`

The detail route returns the lines, their arithmetic and any payment attempts.

### `POST /v1/billing/invoices/{id}/issue` · `.../void`

Issuing writes the ledger entries. Voiding **reverses every entry the invoice
produced** and requires a reason; the invoice row stays, in state `void`. No
financial record is ever deleted.

### `POST /v1/billing/invoices/{id}/payments`

```json
{ "succeeded": false, "failure_code": "card_declined" }
```

Records a collection attempt made **elsewhere** and runs the dunning step from
it. A failure marks the invoice `unpaid` and returns `next_attempt_at`; after
`DUNNING_MAX_ATTEMPTS` it returns `next_attempt_at: null` and
`dunning_exhausted: true` rather than retrying forever. A success marks the
invoice `paid`.

### `GET /v1/billing/ledger`

The append-only ledger, newest first, with the balance each entry produced and
the current receivable and payable. Six kinds — `usage_charge`, `credit`,
`refund`, `tax`, `settlement`, `adjustment` — each a debit or a credit. Amounts
are never negative; `direction` carries the sign. A correction is a
**compensating entry** pointing at the original through `reverses_entry_id`, so
the mistake and the correction are both visible.

### `POST /v1/settlements/run` · `GET /v1/settlements`

```json
{ "period_start": "2026-08-01T00:00:00Z", "period_end": "2026-09-01T00:00:00Z",
  "provider_share_bps": 8000 }
```

Computes your organization's revenue share over **issued** invoices — a draft is
a calculation, not an obligation. A settlement is run for your own organization;
another tenant's earnings are not yours to compute.

The share comes from `REVENUE_SHARE_PROVIDER_BPS` (default 80% to the provider),
is overridable per run, and is **recorded on the settlement**, so changing the
configured default later does not alter what an old settlement says it paid.

A failure leaves the run `paused` with the ledger untouched.

### `POST /v1/settlements/{id}/retry` · `.../payout`

Retry resumes a paused run on the **same row**, so the provider's payable is not
doubled. Payout records a transfer made elsewhere; the response still says
`paid_out_by_this_platform: false`.

---

## Metrics & tracing

### `GET /metrics`

Prometheus exposition. **Disabled by default** — set `METRICS_ENABLED=true`. When `METRICS_TOKEN` is also set, scrapers must send `Authorization: Bearer <token>`; while disabled the endpoint returns 404 rather than advertising itself.

Series are process-level and aggregate: `sutr_tool_calls_total{source,outcome}`, `sutr_tool_call_duration_seconds`, `sutr_tool_calls_gated_total{source,reason}`, `sutr_approval_decisions_total{decision}`, `sutr_http_requests_total{method,route,status}`, `sutr_http_request_duration_seconds`, `sutr_deployments{status}`, `sutr_provider_requests_total{transport,outcome}`, `sutr_provider_request_duration_seconds{transport}`, `sutr_queue_depth{queue}`, `sutr_event_relay_lag_seconds`, and `sutr_telemetry_sample_failures_total`.

They deliberately carry **no org, integration, or tool labels** — that would explode cardinality and leak tenant identifiers to whoever scrapes the endpoint. Per-tenant numbers live in the usage ledger above and in `/v1/observability`. HTTP series are labelled with the matched route *template* (`/api/tools/{integration_id}/call`), never the raw path.

The queue gauges are sampled **on the scrape**, so the value a scrape returns was true when it answered. A sample that cannot read the database increments `sutr_telemetry_sample_failures_total` and leaves the gauges at their previous values — stale rather than zero, because zero is a number an alert would act on.

`sutr_event_relay_lag_seconds` is the age of the oldest event still waiting in the transactional outbox. It is the LLD's "Kafka lag" and "billing lag" for an install where every asynchronous fact leaves through the outbox; there is no broker-level consumer-lag metric.

### Tracing

Optional OpenTelemetry export, off by default. Install the extra (`uv sync --extra otel`) and set `OTEL_ENABLED=true` plus `OTEL_EXPORTER_OTLP_ENDPOINT` (spans are recorded locally if the endpoint is omitted).

Traces span **Gateway → Discovery → Runtime → Provider**, and they span it across processes: W3C trace context (`traceparent`/`tracestate`) is read from the inbound request, so a caller's trace continues here rather than restarting, and is written onto every outbound provider call, HTTP and MCP alike. Sampling is `ParentBased` — a request that arrived sampled stays sampled here whatever `OTEL_TRACES_SAMPLE_RATIO` says locally.

| Span | Kind | `sutr.stage` |
|---|---|---|
| `sutr.gateway` | server | `gateway` |
| `sutr.discovery.search` | internal | `discovery` |
| `sutr.tool_call` | internal | `runtime` |
| `sutr.provider.request` | client | `provider` |

Attributes carry the *shape* of a call and never its content: no arguments, no results, no credentials, and no URLs — a credential can ride in a query string, so the provider span carries `server.address` and the method instead. The discovery span carries the length of the search intent, not the intent. Every span carries `sutr.correlation_id`, and every log line written underneath a recording span carries `trace_id` and `span_id`, which is the join between the log backend and the trace backend.

The correlation id propagates outbound (`X-Correlation-ID`) whether or not tracing is on; `traceparent` goes only while a span is actually recording.

**mTLS to the collector.** `OTEL_EXPORTER_OTLP_CERTIFICATE` (CA bundle), `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE` and `OTEL_EXPORTER_OTLP_CLIENT_KEY` are passed to the OTLP exporter. Both halves of the client certificate are required before the platform reports the hop as mutual — half a certificate is not mTLS, and reporting it as such would be discovered from a collector's rejection log.

A ready-to-run stack — collector, Prometheus, Alertmanager, Loki, Tempo, Grafana, node-exporter, with alert rules, a dashboard and runbooks — lives in `deploy/observability/`. It is single-replica and says so.

---

## Tenant telemetry (`/v1/observability`)

The observability backends are shared: one Prometheus, one Loki, one Tempo, holding every organisation's data. None of them is exposed to a tenant. These routes serve a tenant its **own** telemetry, from this platform's own tables, scoped to the calling organisation by the query itself. All read under `logs:read`, and all return the standard `{data, meta}` envelope.

### `GET /v1/observability/capabilities`

What this install's telemetry actually does — log format and field set, whether `/metrics` is on and authenticated, the tracing configuration (endpoint, sample ratio, TLS, mTLS, propagation), and what tenant telemetry deliberately does not do:

```json
{
  "data": {
    "logging": { "format": "json", "level": "INFO", "redaction": "formatter", "fields": ["correlation_id", "tenant_id", "…"] },
    "metrics": { "endpoint": "/metrics", "enabled": true, "authenticated": false, "tenant_labels": false },
    "tracing": { "enabled": true, "exporter_endpoint": "http://collector:4318/v1/traces", "sample_ratio": 1.0, "collector_tls": false, "collector_mtls": false, "propagation": "w3c-tracecontext" },
    "tenant_telemetry": { "source": "this platform's own call log", "scope": "the calling organisation only", "max_sample": 5000, "metrics_backend_queries": false, "log_backend_queries": false, "trace_backend_queries": false, "detail": "…" }
  }
}
```

### `GET /v1/observability/summary?window_hours=24`

Call rate, error ratio and latency percentiles for the caller's organisation, with a per-provider breakdown:

```json
{
  "data": {
    "window": { "start": "2026-08-31T10:36:36", "end": "2026-08-31T11:36:36" },
    "calls": 2, "truncated": false,
    "by_outcome": { "executed": 2 },
    "errors": 0, "error_ratio": 0.0, "calls_per_minute": 0.033,
    "latency_ms": { "count": 2, "p50": 88, "p95": 128, "p99": 128, "max": 128 },
    "by_provider": { "customapi_example": { "calls": 2, "latency_ms": { "count": 2, "p50": 88, "p95": 128, "p99": 128, "max": 128 } } }
  }
}
```

Percentiles are nearest-rank with no interpolation, so every number returned is a latency that was actually observed. At most 5 000 rows are read; `truncated: true` says when the window held more, rather than describing a fraction of a period as though it were the whole of it. `window_hours` is clamped to `TELEMETRY_WINDOW_HOURS_MAX` (default 720) rather than refused.

### `GET /v1/observability/operations?window_hours=24&limit=50`

The caller's recent operations, newest first — one row per correlation id, because an approval that was granted and then executed is two requests and one operation:

```json
{ "data": { "operations": [ { "correlation_id": "live-check-001", "steps": 1, "started_at": "…", "ended_at": "…", "duration_ms": 128, "trace_id": "b7c50ca2840daa545685013744303d76" } ], "count": 1 } }
```

### `GET /v1/observability/operations/{correlation_id}`

One operation's timeline: each step with its provider, tool, outcome, duration, access reason and (already-redacted) error, plus the `trace_id` an operator can look up in the trace backend — `null` when the call was made with tracing off.

A correlation id belonging to another organisation returns **404**, exactly as one that never existed does. Any other answer would turn this route into a way to ask whether another tenant made a particular call.

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

## Health and readiness

Three endpoints, because an orchestrator asks two different questions. None of
them needs credentials, and none appears in the OpenAPI schema — they are
infrastructure, not API.

### `GET /health`

Unchanged, and an alias for liveness. Existing deployments, the compose
healthcheck and `fly.toml` point here.

```json
{ "status": "ok" }
```

### `GET /health/live`

*Is this process wedged?* Checks the event loop and deliberately nothing else.
A liveness probe that failed during a database outage would restart every
replica at once and turn a recoverable incident into a longer one.

```json
{ "status": "ok", "instance_id": "sutr-api-7f9c:1:9a2b41cd" }
```

### `GET /health/ready`

*Should this replica receive traffic?* Checks the database, and returns **503**
with `"status": "not_ready"` when it cannot be reached — so the replica leaves
the load balancer's rotation while staying alive to be looked at.

```json
{
  "status": "ready",
  "instance_id": "sutr-api-7f9c:1:9a2b41cd",
  "region": "ap-south-1",
  "mode": "active",
  "writes_allowed": true,
  "checks": { "database": { "ok": true, "pool": { "dialect": "postgresql", "size": 5, "checkedin": 1, "checkedout": 0 } } },
  "leadership": ["deployment_monitor", "event_relay", "maintenance", "source_sync"]
}
```

`leadership` is which background jobs *this* replica currently owns — the
answer to "which one is sweeping?". A standby reports `ready` with
`writes_allowed: false`: serving reads is what it is for.

Connection details never appear here. A failed check reports
`{"ok": false, "detail": "unreachable"}` and the host, user and port go to the
log, because this endpoint is unauthenticated.

---

## Failure handling (`/v1/resilience`)

### `GET /v1/resilience/policy`

The failure-handling policy this build was assembled with: timeout, retry and
circuit-breaker settings, the eight failure domains with the severity and what
survives each, and — in `not_implemented` — what §5.8 asks for and this build
does not do.

```json
{
  "data": {
    "timeouts": { "overall_seconds": 30.0, "connect_seconds": 5.0, "read_seconds": 30.0, "pool_seconds": 5.0 },
    "retries": { "attempts": 2, "jitter": "full", "retries": "transport failures on safe methods only" },
    "circuit_breaker": { "enabled": true, "failure_threshold": 5, "cooloff_seconds": 30.0, "scope": "per process", "half_open_trial_calls": 1 },
    "severities": { "P0": "The platform is not serving agent traffic.", "…": "…" },
    "domains": [ { "name": "external_provider", "severity": "P2", "effect": "One provider's tools fail.", "survives": "Every other provider. …", "implemented_in": "resilience/breaker.py" } ],
    "not_implemented": { "bulkheads": "There are no isolated pools per workload. …" }
  }
}
```

### `GET /v1/resilience/providers`

Circuit state and health score per provider, **as this replica sees it**. The
breaker is per process, so another replica may be serving a provider this one
has quarantined — deliberately: one replica's bad network should not take a
provider away from every other.

```json
{
  "data": {
    "providers": [
      { "provider": "api.example.com", "state": "open", "score": 0, "successes": 0, "failures": 5,
        "consecutive_failures": 5, "last_error": "ConnectTimeout", "quarantined": true, "seconds_in_state": 12.4 }
    ],
    "quarantined": ["api.example.com"],
    "scope": "this replica only"
  }
}
```

A provider nothing has been observed about reports `"score": null` rather than
100 — nothing observed is not the same as healthy.

While a circuit is open, a call to that provider fails immediately with a named
error rather than waiting out the timeout. Measured on a live instance: 10.1
seconds per call before, 9 milliseconds after.

### `POST /v1/resilience/providers/{provider}/close`

Close a circuit now, for an operator who has fixed the upstream rather than
waiting out the cool-off. Requires `org:manage`, is audited, and returns 404 if
that provider has no open circuit on this replica.

---

## Running more than one replica (`/v1/platform`)

### `GET /v1/platform/mode`

Whether this instance may write, and which region owns writes if not.

```json
{
  "data": {
    "mode": "standby",
    "writes_allowed": false,
    "region": "ap-south-2",
    "primary_region": "ap-south-1",
    "read_only_writes": ["/v1/discovery/evaluate", "/v1/discovery/search", "/v1/governance/evaluate"]
  }
}
```

On a standby, every write is refused with **503** rather than being allowed to
fail inside the driver:

```json
{
  "error": {
    "code": "unavailable",
    "message": "This instance is a read-only standby (ap-south-2). Writes are served by ap-south-1.",
    "details": { "mode": "standby", "region": "ap-south-2" },
    "retry_after": 60
  }
}
```

`read_only_writes` are the POST routes that write nothing — a query too
structured for a URL is still a query — and are served normally on a standby.
`primary_region` is recorded configuration, not a discovery: this platform does
not elect a primary, and reporting a guess would be worse than reporting
`null`.

### `GET /v1/platform/leadership`

Which replica runs each job that must run exactly once.

```json
{
  "data": {
    "instance_id": "sutr-api-7f9c:1:9a2b41cd",
    "jobs": ["maintenance", "deployment_monitor", "source_sync", "event_relay"],
    "held": ["maintenance"],
    "holders": { "maintenance": "sutr-api-7f9c:1:9a2b41cd", "event_relay": "sutr-relay-2b1a:1:44ff0912" },
    "lease_seconds": 45,
    "renew_seconds": 15
  }
}
```

`held` is what this replica owns; `holders` is what every job's lease says. A
leader that dies is replaced once its lease expires — up to `lease_seconds +
renew_seconds`, measured at 61 seconds after a SIGKILL. A clean shutdown hands
the lease back and the takeover is immediate.

### `GET /v1/platform/scaling`

What is still per-process when this runs on more than one replica — rate-limit
windows, the discovery cache, approval long-poll waiters, the metrics registry
— each with what changes and what to do about it. Nothing on the list is a
correctness bug; work that must not run twice runs under a lease instead.
