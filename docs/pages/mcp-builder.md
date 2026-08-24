---
title: MCP builder
nav_title: MCP builder
---
# MCP builder

The builder turns an OpenAPI specification into governed agent tools, one visible stage at a time. Each stage names what the compiler actually did, so a failure is attributable to a stage rather than to "the import".

**Integrations → New → Build from an API**, or go straight to `/app/integrations/openapi/new`.

```
Source → (Choose file) → Normalize & IR → Select tools → Authentication → Build → Deploy
```

Nothing is created until the Build stage, and deploying is optional and separate.

> The hand-rolled **Custom API** builder was removed. The OpenAPI path produces the same integration rows but derives the paths, parameters, and auth from a specification instead of asking you to retype them, so it is strictly better for the same job. `/app/integrations/custom-api/new` redirects here.

---

## Stage 1 — Source

Five ways in. Sutr fetches everything server-side, validates it against the official OpenAPI 3.0/3.1 schemas, resolves `$ref`s cycle-safely, and refuses external `$ref`s. Swagger 2.0 is rejected — convert it first.

### GitHub

Pick how to authorize:

- **Connected account (OAuth)** — the recommended path. [Connect GitHub once](/connected-accounts) and the builder lists your repositories, private ones included, most recently updated first. Choosing one fills in the URL. Nothing is typed and nothing is pasted.
- **Personal access token, or none at all** — public repositories need no credential. A token is only for private repos or for getting past GitHub's unauthenticated rate limit; create one at *GitHub → Settings → Developer settings → Personal access tokens* with read access to repository contents. **It is used for that one request and never stored** — re-enter it if you import again.

The **Repository or file URL** field accepts every shape people actually paste:

| URL | What Sutr does |
|---|---|
| `https://github.com/owner/repo` | Resolves the default branch and searches the whole tree |
| `https://github.com/owner/repo/tree/main/specs` | Searches only that folder |
| `https://github.com/owner/repo/blob/main/openapi.yaml` | Uses exactly that file |
| `https://raw.githubusercontent.com/owner/repo/main/openapi.json` | Uses exactly that file |

Pasting a GitHub or SwaggerHub link into the **Direct URL** source works too — Sutr detects it and routes it through the right adapter, rather than fetching the HTML page and failing on a parse error that explains nothing.

### SwaggerHub

Open the API in SwaggerHub and copy the address bar: `https://app.swaggerhub.com/apis/{owner}/{api}/{version}`. `app.`, `portal.`, and `api.swaggerhub.com` hosts are all accepted, and the version is optional.

The **API key** is only needed for a private API (SwaggerHub → your avatar → *API Key*). SwaggerHub expects the key **as-is, not as a Bearer token**, so paste it exactly; Sutr sends it unprefixed. Used for that one request and never stored. Sutr also asks SwaggerHub to inline external `$ref`s before returning the document, since the resolver refuses external references outright.

### Upload

Drag a file in, or click to choose one. Up to 8 MB, `.json` / `.yaml` / `.yml`.

The file is read **in your browser** and its text is sent to your Sutr server with the import — it is not uploaded to any third party, and the file itself is never stored. The filename travels along as provenance, so a project imported this way still records where it came from.

### Direct URL

Any publicly readable specification URL, e.g. `https://petstore3.swagger.io/api/v3/openapi.json`.

The URL must be reachable **from the Sutr server**. It is SSRF-screened: private, loopback, and link-local addresses are refused, redirects are **not** followed (use the final URL), and the response is size-capped.

### Paste

The whole document, JSON or YAML. For a file on your machine, use Upload instead.

**Credentials are pinned to their provider's host.** A GitHub token is only ever attached to a request whose host already resolved to GitHub, and likewise for SwaggerHub; there is a test that asserts it.

---

## Stage 2 — Choose file

Shown only when a repository holds several candidates. Sutr ranks them best-first — `openapi.*` before `swagger.*` (a `swagger.json` is often a generated artifact), shallower paths before deeper ones, YAML before JSON, and merely spec-shaped names last — but the first entry is a **default selection, not a decision**. Pick any of them.

---

## Stage 3 — Normalize & IR

The parsed, validated, normalized intermediate representation: title, version, OpenAPI version, servers, security schemes, operation count, and every warning the normalizer raised. Warnings do not stop an import; they tell you what the specification left ambiguous.

---

## Stage 4 — Select tools

Every operation, filterable by tag and by text, all selected by default — narrowing is a deliberate act. Deprecated operations are excluded unless you say otherwise.

Operations **without an `operationId`** are marked and explained: the compiler's filters key on `operationId`, so those can only be excluded by tag.

---

## Stage 5 — Authentication

How the deployed tools authenticate to the upstream API. Sutr pre-fills this by translating the specification's `securitySchemes`, and you can override it:

- **Header name** — e.g. `Authorization`, `X-Api-Key`.
- **Token format** — a template containing the literal `{token}` placeholder, e.g. `Bearer {token}` or just `{token}`. It is validated, so a format without the placeholder is refused rather than silently sending a header with no credential.

The credential itself is not entered here. Tools compiled into an integration take it from the integration's connection; a deployed standalone server takes it from an environment variable.

Header `apiKey` and HTTP `bearer`/`basic` schemes translate. Query and cookie `apiKey`, and real OAuth2 grants, are **not** supported for compiled APIs — the import warns rather than compiling something that cannot authenticate.

---

## Stage 6 — Build

Choose the **base URL** every tool path is appended to. It comes from the specification's `servers` block; override it to point at a different environment (sandbox versus production), and fill in any `{server variables}`. Private and loopback addresses are refused here and re-validated on every call.

Then:

- **Preview tools** — the exact compiled tools, their names (deterministic from `operationId`, with documented collision renaming), methods, paths, and parameter counts. Nothing is created.
- **Create integration** — registers the tools in your catalog. Every tool starts in **require approval** mode; connect the credential and relax policy per tool on the integration page.
- **Package** — downloads a self-contained standalone MCP server as a zip: `server.py`, a runtime with no Sutr dependency, `tools.json`, generated offline tests, a `Dockerfile`, and a `.env.example`. Runs anywhere you can run Python or Docker.

---

## Stage 7 — Deploy

Optional. Runs the generated server as a standalone MCP endpoint. **A deployed server talks to the upstream API directly and does not route through Sutr, so Sutr's approval policy does not apply to it.** Use the integration when you want governed tools; use a deployment when you want a plain MCP endpoint of your own.

| Target | What it creates | Reachable at |
|---|---|---|
| **Local Docker** | A container on the Sutr host, bound to `127.0.0.1` | `http://127.0.0.1:{port}/mcp` |
| **Google Cloud Run** | Artifact Registry repo (if missing) → Cloud Build build → public Cloud Run service | `https://…run.app/mcp` |
| **Azure Container Apps** | Resource group, registry, environment (each if missing) → ACR Tasks build → Container App | `https://….azurecontainerapps.io/mcp` |
| **AWS App Runner** | S3 bucket and ECR repo (if missing) → CodeBuild build → App Runner service | `https://….awsapprunner.com/mcp` |

All three cloud targets build a container image with **that cloud's own build service** and run it on its serverless container runtime, scaling to zero when idle. All three are authorized by [a connected account](/connected-accounts), so a deployment can only ever touch what you could already touch yourself.

The form for each provider is rendered from what the provider declares it needs, so what you see is what will be sent:

- **Google Cloud** — project (picked from your own project list), region, and the Artifact Registry repository name.
- **Azure** — subscription (picked from your own), region, resource group, container registry name (globally unique, 5–50 lowercase alphanumeric characters, no hyphens), and the Container Apps environment.
- **AWS** — account and permission set (both picked from your Identity Center assignments), region, plus two IAM role ARNs the account needs once: a CodeBuild service role and an App Runner ECR access role. See [the setup guide](/self-host/connected-accounts#amazon-web-services--deploying-to-app-runner) for their exact policies.

The **upstream API token** is optional and is stored through Sutr's secrets backend, then injected as an environment variable at run time — never written into the package or baked into the image.

Cloud builds take a few minutes. The deployment appears immediately with status `queued`, moves to `building`, and lands on `running` or `failed` with the real reason. Progress, the MCP URL, and logs are on the **Deployments** page.

### Stop, start, delete

- **Cloud Run** has no "stopped" state — it already scales to zero — so *stop* switches ingress to internal-only, which makes it genuinely unreachable without destroying the revision, and *start* puts it back.
- **Container Apps** and **App Runner** have real stop/pause operations and use them.
- **Delete** removes only what belongs to that deployment. Shared resources — the resource group, registry, environment, bucket, ECR repository — are left alone, because tearing them down with one deployment would break the others.

### Known gaps

- **Azure logs** live in the environment's Log Analytics workspace, which is a different API with a different token audience than the deploy grant covers. `logs` points at the portal instead of returning an empty string that would read as "the server printed nothing". Cloud Run and App Runner logs are fetched normally.
- The cloud providers are implemented against each cloud's documented REST APIs and covered by tests at the HTTP layer, but have not been run end to end against live paid accounts. The local Docker provider has.

---

## From the CLI

```bash
sutr connections connect github
sutr openapi discover https://github.com/owner/repo --use-connection
sutr openapi import --url https://github.com/owner/repo --path openapi.yaml --use-connection
sutr openapi import --file ./openapi.yaml          # imported as an upload, filename kept
sutr openapi compile <project_id> --dry-run
sutr deploy create --project <id> --name Petstore --provider gcp \
  --connection <connection_id> --config project=acme-prod --config region=us-central1
```

`--config` is repeatable and its keys are whatever the chosen provider declares; `sutr deploy providers` prints the list. Credentials also come from `--github-token` / `--swaggerhub-key` or the `SUTR_GITHUB_TOKEN` / `SUTR_SWAGGERHUB_API_KEY` environment variables. The CLI does not re-implement source detection — the server owns that rule.
