# ESDS Sovereign Agent Platform — Implementation Gap Analysis

**Phase 0 deliverable.** Read-only audit of `sanketwork300-hash/sutr1` against
*ESDS Sovereign Agent Platform — Developer LLD (Condensed Edition v1.1, 30 pp.)*.
No code was changed to produce this document.

- **Date:** 2026-08-27
- **Baseline verified:** `uv run pytest -q` → **953 passed** in 67.6 s; 29 Alembic revisions
  (`server/alembic/versions/0001…0029`); ruff clean.
- **Repository size at audit:** 224 Python modules under `server/src/sutr/`, 29 API routers,
  34 UI pages, 8 CLI command groups, 2 SDKs.

This document answers one question per LLD section: *what does Sutr already do, what does the
LLD ask for, and what is the delta?* Statuses use the vocabulary mandated by the build prompt
§83: **IMPLEMENTED · PARTIAL · EXPERIMENTAL · BLOCKED · NOT TESTED · NOT IMPLEMENTED**.

---

## 0. Executive summary

Sutr today is a **single-process, single-plane tool gateway**: a FastAPI monolith that is
simultaneously an MCP server (StreamableHTTP gateway at `/mcp`), an MCP client (toward upstream
remote servers), an OAuth 2.1 authorization server, an OpenAPI→MCP compiler, and a deployment
orchestrator for generated standalone servers. It is genuinely good at that job and it is tested.

The ESDS LLD asks for a **two-plane, event-driven, polyglot-persistence platform**: 15 control-plane
services behind Kafka, a latency-critical stateless data plane that survives control-plane outages,
documentation intelligence, a knowledge graph, hybrid semantic discovery, scoped access passes,
a policy decision point, immutable financial ledgers, and Kubernetes-hosted per-provider runtimes.

The honest headline:

| | |
|---|---|
| **Sutr covers, in a directly reusable form** | identity, OAuth (both directions), API keys, RBAC, orgs/workspaces, the OpenAPI compile pipeline, the canonical execution pipeline, approvals/governance-lite, secrets abstraction, deployment providers, the usage ledger, Prometheus/OTel wiring, SDKs, CLI, UI |
| **Sutr has no equivalent of** | Kafka/event bus, the tool lifecycle state machine, documentation intelligence, knowledge graph, vector search, discovery/ranking, trust score, risk engine, compliance engine, PDP/PEP separation, scoped access passes, quotas, invoices/settlement, Kubernetes/GitOps runtime management, drift detection, multi-region |
| **Sutr contradicts the LLD and must be reconciled deliberately** | one database (SQLite/Postgres) vs database-per-service; monolith vs 15 services; org/workspace vs provider/tenant; synchronous everything vs async control plane |

The reconciliation strategy adopted (see `ARCHITECTURE_DECISIONS.md`, ADR-001 and ADR-002) is
**modular monolith first**: introduce the LLD's service boundaries as Python packages with their own
API/service/domain/repository layers and their own event contracts, keep one deployable, and make
the transport (in-process bus vs Kafka) a configuration choice. This satisfies the build prompt's
explicit instruction — *"Initially these may remain inside the Sutr monorepo as modular services.
DO NOT prematurely split them into 15 deployments."*

---

## 1. Plane model (LLD §2.2, §3.1, §3.2)

**LLD requirement.** Control Plane = tool lifecycle (onboard → translate → generate → deploy →
govern → publish), low volume, seconds–minutes acceptable, failure blocks *new* tools only. Data
Plane = live agent invocation, stateless, sub-second, must survive Control Plane outages.

**Sutr today.** No plane distinction exists. `services/tool_pipeline.py` (the canonical execution
path, 487 lines) is data-plane work; `api/openapi_projects.py` (597 lines) and
`services/deployments.py` are control-plane work; both run in the same process, share one database
session factory, and share one failure domain. A migration that locks `openapi_project` would stall
tool invocation.

**Delta.** Package boundaries + a documented rule that data-plane code may not import control-plane
modules. Cache the data needed on the hot path (tool resolution, policy decision, runtime endpoint)
so that control-plane unavailability is survivable — this is the one LLD guarantee that has to be
*tested*, not asserted.

**Status: NOT IMPLEMENTED** (planes are not separated).

---

## 2. Control-plane pipeline & state machine (LLD §3.1, §4.1)

**LLD requirement.** 14 states (`DRAFT → API_UPLOADED → TRANSLATING → IR_READY → DOC_PROCESSING →
METADATA_READY → GENERATING_MCP → VALIDATING → DEPLOYING → DEPLOYED → UNDER_REVIEW → APPROVED →
PUBLISHED → ACTIVE`) plus failure branches (`TRANSLATION_FAILED`, `DOCUMENTATION_FAILED`,
`GENERATION_FAILED`, `DEPLOYMENT_FAILED`, `REJECTED`, `SUSPENDED`, `ARCHIVED`), each stage
persisting state, emitting an event, retryable and resumable from the last good stage.

**Sutr today.** `models/openapi_project.py` (42 lines) holds an OpenAPI project with `ir_json` and a
`status` string, driven synchronously by `api/openapi_projects.py`. `models/deployment.py` has its
own status field. There is no shared lifecycle vocabulary, no resume, no per-stage event, no
correlation ID threaded through stages (`request_context.py` carries a request ID for one HTTP
request only).

**Delta.** A `ToolLifecycleState` enum shared platform-wide; a stage-runner that persists
`(entity, stage, state, correlation_id, attempt, last_error)` before and after each stage; retry and
resume entry points; one event emitted per transition.

**Status: NOT IMPLEMENTED.**

---

## 3. Event bus (LLD §5.6)

**LLD requirement.** Kafka; immutable versioned self-describing schema-validated events; envelope
with `event_id`, `event_type`, `event_version`, `timestamp`, `correlation_id`, `tenant_id`,
`resource_id`, `producer`, `payload`; partition by `provider_id` / `tool_id` / `runtime_id` /
`policy_id` / `invoice_id`; at-least-once with idempotent consumers; per-domain retry → DLQ;
Apicurio + Avro.

**Sutr today.** Zero. The only asynchronous machinery is `mcp/notifications.py` — a single-process
asyncio pub/sub for approval long-polls, explicitly documented as breaking under multiple workers —
and `maintenance.py`, an in-process loop for cache refresh and retention.

**Delta.** Everything. The pragmatic path is an `events/` package with an `EventBus` protocol, an
envelope model, a durable outbox table (so events survive a crash between commit and publish), an
in-process/DB-backed default implementation, and a Kafka implementation selected by configuration.
Contracts live in `contracts/events/`.

**Status: NOT IMPLEMENTED.**

---

## 4. Source connectors (LLD §3.3)

**LLD requirement.** `connect()`, `discover()`, `fetch()`, `validate()`, `watch()`,
`detect_drift()`, `disconnect()`. Bootstrap + continuous sync. Sources: file upload, paste, OpenAPI
URL, Swagger-UI URL, WSDL URL, Git repositories, SwaggerHub, API gateways, API registries, Postman
collections. Drift classified and gated by policy.

**Sutr today.** `openapi/sources.py` (465 lines) implements **paste, upload, direct URL, GitHub
(repo/tree/blob/raw + discovery + OAuth repo picker), SwaggerHub** — five sources, end to end, with
SSRF screening and provenance recorded on the project row. This is a real, working subset and the
strongest existing foundation for this LLD section.

**Delta.** The abstraction is source-shaped but not connector-shaped: there is no interface with the
seven verbs, no `watch()`, no stored `source_version`/`commit_sha`/`etag`/`last_modified`, no drift
detection, no classification (`BREAKING`/`NON_BREAKING`/`SECURITY`/`DOCUMENTATION`/`METADATA`), and
no second-source comparison — which the LLD calls out as *"the governance differentiator"*.
Swagger-UI-URL auto-discovery, WSDL, Postman, and gateway/registry connectors are absent.

**Status: PARTIAL** (5 of ~10 source types; 3 of 7 verbs; no sync, no drift).

---

## 5. Translation service & IR (LLD §3.4)

**LLD requirement.** Specification → AST → Semantic Graph → Normalized Graph → IR, each stage
inspectable; deterministic (same spec ⇒ identical IR); versioned artifacts; 10,000+ operations per
spec; auth schemes API Key, OAuth2 (CC + Auth Code), Basic, Bearer, mTLS; secrets never in IR;
events `api.uploaded → translation.started → translation.completed`.

**Sutr today.** `openapi/` is a genuine, tested compile pipeline: `loader.py` (JSON/YAML, size cap,
SSRF-screened fetch) → `normalizer.py` (official schema validation, `$ref` resolution via
`resolver.py` with cycle placeholders and depth budgets, → `ApiDefinition` IR) → `compiler.py`
(filtering, deterministic 3-step tool naming, param mapping, honest descriptions) →
`security.py` (auth translation). The IR is persisted as `ir_json` on the project row. Secrets are
not in the IR. Determinism is structurally true (pure functions) but not asserted by a test.

**Delta.**
- **Swagger 2.0 rejected outright** (`normalizer.py:validate_spec`) — LLD and build-prompt §17 both
  require conversion.
- **Single-error reporting**: `openapi_spec_validator` raises on the first failure; LLD §3.4 and
  build-prompt §16 require Spectral-style multi-finding diagnostics with `rule_id`, `severity`,
  `location`, JSON pointer, remediation.
- **Auth coverage**: header `apiKey`, `http bearer`, `http basic` are real; **query and cookie
  `apiKey` are dropped with a warning**; **oauth2/openIdConnect silently degrade to a pasted bearer
  token** — the build prompt §24 forbids exactly this degradation; mTLS absent; only one scheme can
  be active.
- **Body coverage**: only `application/json` and `+json`. Form-urlencoded, multipart, and binary
  bodies emit `unsupported_body` and are dropped (`normalizer.py`).
- **Scale**: `MAX_OPERATIONS = 300`, `MAX_GENERATED_TOOLS = 200` (`openapi/limits.py`) against the
  LLD's 10,000+. These caps are deliberate DoS protection for untrusted input and should be raised
  per-tier rather than removed.
- No AST/semantic-graph stage separation, no IR versioning, no IR diffing.

**Status: PARTIAL** (strong core; four named coverage gaps).

---

## 6. Documentation intelligence (LLD §3.5)

**LLD requirement.** Parse (PDF/HTML/DOCX/MD/TXT, OCR fallback) → semantic chunk → parallel
extraction → business rules, workflows, glossary, knowledge graph, embeddings → metadata. Storage:
Object Storage (originals), PostgreSQL (state), Neo4j (graph), Qdrant/pgvector (embeddings).
Checkpoint per stage; OCR failure flags pages and continues; graph failure continues with a warning.
Events `documentation.uploaded → parsing.completed → knowledge.extracted → embeddings.generated →
documentation.processed`.

**Sutr today.** Nothing. No `documentation/` package, no document model, no chunking, no extraction,
no embeddings, no graph.

**Delta.** The entire subsystem, including its external dependencies (Unstructured/Tesseract/spaCy
per the LLD stack, an embedding provider, Neo4j, Qdrant). Every one of those is an optional
dependency that must degrade rather than break the platform.

**Status: NOT IMPLEMENTED.**

---

## 7. Metadata service & trust score (LLD §3.7, §4.1 step 6)

**LLD requirement.** AI metadata per tool — intent, category, risk, capabilities, required roles,
estimated latency, data sensitivity, region, compliance, pricing, documentation quality, runtime
availability, trust score (0–100, explainable, influences ranking), embedding, workflow and
business-rule references, lifecycle state.

**Sutr today.** Tools carry `name`, `description`, `inputSchema`, and an optional UI-only
`tool_categories` map on bundled integrations. `services/tool_catalog.py` unifies discovery and
caching across bundled/custom/compiled sources. No semantic metadata, no scoring.

**Status: NOT IMPLEMENTED.**

---

## 8. MCP generator & runtime (LLD §3.6, §2.5)

**LLD requirement.** IR + documentation knowledge + runtime template → MCP server → validation →
immutable artifact. Templates for Python, Go, Node.js. Generated middleware chain Authentication →
Validation → Logging → Execution → Metrics → Response. Artifact carries hash, generator version, IR
version, SBOM, dependency lock, validation result, scan result, deployment manifest. Signed images,
isolated build workers, reproducible builds.

**Sutr today.** `openapi/packaging.py` (595 lines) emits a **deterministic 9-file Python package**
(fixed zip timestamps), with `server.py` (stdio + StreamableHTTP), a self-contained
`sutr_runtime.py` that mirrors gateway request semantics exactly, `tools.json` as the only
API-specific data (so a hostile spec cannot inject code), a generated offline pytest suite,
Dockerfile, README, `.env.example`. This is a real, defensible implementation of the *generation*
half.

**Delta.** Python only (no Go/Node templates). No SBOM, no signing, no artifact hash record, no
dependency lock, no validation-result artifact, no security scan. Generated middleware has logging
and execution but **no authentication or authorization at all** — build-prompt §8/§24 and LLD §4.3
require JWT with issuer/audience/expiry/nonce, tenant/agent/tool/scope validation. Today a generated
server trusts any caller that can reach it, which is why `deploy/registry.py` disables the local
Docker provider on multi-tenant instances and binds to 127.0.0.1.

**Status: PARTIAL** (generation IMPLEMENTED for Python; artifact integrity and generated-server
security NOT IMPLEMENTED).

---

## 9. Runtime manager, Kubernetes, GitOps (LLD §3.2, §4.1 step 9, §5.7)

**LLD requirement.** Runtime Manager owns build/deploy/status/health/start/stop/update/rollback/
delete/logs/metrics/config/secrets/versions/regions/limits. Runtime states `CREATED → READY →
RUNNING → SCALING → UPDATING → TERMINATING → ARCHIVED` (+ `FAILED`, `QUARANTINED`). Kubernetes
objects: Namespace, Deployment, Service, ServiceAccount, ConfigMap, Secret refs, HPA, Ingress,
NetworkPolicy, probes, resource limits, Pod Security Standards. GitOps via Argo CD with drift
detection and one-command rollback.

**Sutr today.** `deploy/base.py` defines a clean `DeploymentProvider` ABC —
`available/deploy/status/start/stop/remove/logs`, each taking a `ProviderTarget` of freshly resolved
credentials — with four implementations: `docker_provider.py`, `gcp_provider.py` (Cloud Build →
Artifact Registry → Cloud Run), `azure_provider.py` (ACR Tasks → Container Apps),
`aws/provider.py` (S3 → CodeBuild → ECR → App Runner). Providers declare their own `config_fields`
so the UI renders a form without knowing the provider. Secrets are injected as env vars at deploy
time, never baked into the image.

**Delta.**
- **No `update()` and no `rollback()` on the interface.** Today an update is delete-and-recreate,
  which loses the URL and the history. Build prompt §36 forbids this.
- **No `metrics()`.** Status polling exists; CPU/memory/request-count/latency/replica collection
  does not. Azure logs return a portal link because Log Analytics is a different API with a
  different token audience — documented honestly, still a gap.
- **No Kubernetes provider, no GitOps provider.**
- **No runtime state machine** — `models/deployment.py` has a flat status string.
- **No versioned deployment artifacts, no deployment history.**
- **Swaraj Cloud: the name exists only in comments.** Per build prompt §7 and §4, no API may be
  invented. A provider stub returning `NOT_CONFIGURED / DOCUMENTATION_REQUIRED` is the correct
  deliverable.

**Status: PARTIAL** (4 providers IMPLEMENTED but NOT TESTED against live paid cloud accounts —
only the local Docker provider has been exercised live; update/rollback/metrics/K8s/GitOps NOT
IMPLEMENTED; Swaraj **BLOCKED — EXTERNAL API DOCUMENTATION REQUIRED**).

---

## 10. Registry & marketplace (LLD §3.7)

**LLD requirement.** Registry = authoritative system of record: tool, provider, version, IR ref,
runtime, artifact, deployment, governance, trust score, visibility, region, pricing, subscription,
documentation, compliance, SBOM, build hash. Lifecycle `DRAFT → GENERATED → VALIDATED → DEPLOYED →
UNDER_REVIEW → APPROVED → PUBLISHED → ACTIVE → DEPRECATED → ARCHIVED`, versions immutable, old
versions kept for rollback. Marketplace = read-optimized storefront derived from Registry events:
catalog, categories, tags, provider profiles, ratings, reviews, pricing, subscriptions, install
counts. Subscription lifecycle Discover → Subscribe → Provision → Use → Renew → Cancel.

**Sutr today.** `integrations/registry.py` merges bundled + custom MCP + compiled OpenAPI
integrations into one catalog; the UI has a browse grid with text search. That is a *catalog*, not a
*registry*: no versions, no lifecycle, no immutability, no governance state, no derived storefront.

**Status: NOT IMPLEMENTED** (registry); **PARTIAL** (a browse grid exists where a marketplace is
required).

---

## 11. Discovery engine (LLD §3.8)

**LLD requirement.** Natural-language intent → policy filter → lexical (OpenSearch) + vector
(Qdrant) + graph (Neo4j) hybrid retrieval → configurable versioned ranking → ranked results. Policy
filter runs **before** ranking. Degradation: graph down ⇒ skip expansion; embedding failure ⇒
lexical only; ranking timeout ⇒ order by semantic similarity; index lag ⇒ serve stale and flag.
Cache key = tenant + intent + policy version. Quality tracked by precision/recall/NDCG.

**Sutr today.** `mcp/management_tools.py` exposes a `search` meta-tool that does substring matching
across cached tool names and descriptions. No embeddings, no ranking, no policy pre-filter.

**Status: NOT IMPLEMENTED** (substring search is not semantic discovery).

---

## 12. Provisioning & scoped access passes (LLD §4.3)

**LLD requirement.** Agents never hold permanent provider credentials. Provisioning issues
short-lived, single-purpose, least-privilege, signed, revocable passes carrying tenant, agent, tool,
operations, region, expiry, policy version, scope, nonce. The runtime validates the pass.

**Sutr today.** Agents authenticate with org-wide `ap_` API keys (`api/api_keys.py`) that are
unscoped and have no expiry. Provider credentials are held by the platform and injected server-side
at dispatch — so the *credential-leak* property the LLD wants is partly satisfied by construction —
but there is no pass, no scoping, no expiry, no nonce, no revocation primitive.

**Status: NOT IMPLEMENTED.**

---

## 13. Governance, PDP/PEP, compliance, risk (LLD §5.2)

**LLD requirement.** Continuous governance across upload → validation → generation → deployment →
publication → invocation → monitoring. Declarative versioned policies with lifecycle `Draft → Review
→ Approved → Published → Active → Deprecated → Archived`. PDP decides centrally, PEPs enforce
everywhere. Compliance engine (ISO 27001, SOC 2, GDPR, HIPAA, PCI DSS, RBI Digital Banking, India
DPDP Act). Risk engine, explainable, stale-on-failure. Approval workflow with security/business/legal
reviews and expiring exceptions. Separation of duties: no admin may author and approve the same
policy. Fail closed for high-risk operations.

**Sutr today — genuinely strong for its scope.** `approvals/policy.py` evaluates deny-by-default
per-tool policy (`allow | require_approval | deny`); `approvals/requests.py` implements approve-once
with SHA-256 argument binding, approve-exact-forever, and allow-tool-forever; TOTP can gate
decisions; `services/audit.py` writes an audit trail in the caller's session so an audited action
cannot commit unaudited; `services/redaction.py` keeps secrets out of logs; `authz.py` is a 5-role ×
11-permission matrix.

**Delta.** Governance applies at **invocation only** — nothing gates upload, generation, deployment,
or publication. Policies are rows, not versioned declarative documents with a lifecycle. There is no
PDP/PEP separation (evaluation is inline in the pipeline). No compliance engine, no risk engine, no
review workflow, no exceptions, no separation-of-duties rule.

**Status: PARTIAL** (invocation-time governance IMPLEMENTED and well tested; the other six
governance points and all three engines NOT IMPLEMENTED).

---

## 14. Billing, metering, quotas (LLD §5.1)

**LLD requirement.** Immutable append-only usage events; prices evaluated at billing time, never
during execution; models Free/Per Invocation/Per API Call/Per Second/Subscription/Tiered/Hybrid/
Enterprise; subscription states `TRIAL → ACTIVE → SUSPENDED → EXPIRED → RENEWED`; quotas (daily,
monthly, concurrency, tokens, transfer) **checked before execution**; immutable ledger (usage
charge, credit, refund, tax, settlement, adjustment); revenue share; dunning; dedupe by invocation
ID + idempotency key; billing never blocks the agent.

**Sutr today.** `models/usage_event.py` + `services/metering.py` write a durable ledger row **in the
same transaction as the execution log** — an execution can never be logged unmetered nor metered
without running. That is a correct and valuable foundation. Stripe customer/checkout/portal/webhook
work (`billing/`), with webhook idempotency via `processed_stripe_event`.

**Delta.** `grep -rn quota server/src/` returns **one** incidental match. There is no quota model, no
pre-execution check, no 429 path, no pricing engine, no invoice, no ledger of financial entries, no
settlement, no revenue share, no dunning. The only limit is `FREE_INTEGRATION_LIMIT = 5` installed
integrations. Metering is synchronous with execution (correct for durability, but the LLD's async
billing consumption does not exist because there is no bus).

**Status: PARTIAL** (metering IMPLEMENTED; quotas, pricing, invoicing, settlement NOT IMPLEMENTED).

---

## 15. Identity & zero trust (LLD §4.3)

**LLD requirement.** Every user, agent, service and runtime has an identity; every request
authenticated, authorized, encrypted, logged. TLS 1.3, mTLS service-to-service, short-lived JWTs
with nonce validation, Vault + KMS, RBAC/ABAC, NetworkPolicy, ServiceAccounts, namespace isolation,
resource quotas, Pod Security Standards. Threat table: credential theft, token replay, cross-tenant
access, unauthorized invocation, supply chain, secret leakage, lateral movement, compromised
runtime, DDoS, audit tampering.

**Sutr today.** Human identity is solid: signup/login, email verification, password reset, TOTP 2FA
with recovery codes, Google SSO, login lockout, OAuth 2.1 AS with DCR/PKCE/refresh rotation/
revocation. `upstream_safety.py` blocks SSRF to loopback/private/link-local with no redirects.
`secrets/` abstracts a `db` (plaintext default) and `db_kms` (AES-256-GCM envelope) backend.

**Delta.** **No agent identity distinct from the human/org** — an API key is the only agent-ish
principal and it is org-wide and permanent. No service identity, no runtime identity, no mTLS, no
Vault, no nonce/replay protection, no ABAC. Secrets default to plaintext in the database. SSRF has a
known DNS-rebinding TOCTOU window (resolved IPs are not pinned).

**Status: PARTIAL** (human identity IMPLEMENTED; agent/service/runtime identity and the zero-trust
controls NOT IMPLEMENTED).

---

## 16. Observability (LLD §5.3)

**LLD requirement.** OTel + Prometheus + Grafana + Loki + Tempo + Alertmanager + Fluent Bit;
tenant-isolated telemetry; PII masking; traces spanning Gateway → Discovery → Runtime → Provider;
**every log line carries correlation ID, agent ID, provider ID, tool ID, runtime ID, status,
duration**.

**Sutr today.** `observability/metrics.py` exposes a deliberate, well-reasoned Prometheus surface
(no tenant labels — documented as a cardinality and disclosure decision, with per-tenant numbers in
the usage ledger instead). `observability/tracing.py` provides real OTel spans, off by default.
`request_context.py` carries a per-request correlation ID.

**Delta.** **Logging is plain text** — `main.py` calls `logging.basicConfig` with a text format.
The LLD's mandatory per-line field set does not exist. No Loki/Tempo/Grafana/Alertmanager
deployment assets.

**Status: PARTIAL** (metrics IMPLEMENTED, tracing IMPLEMENTED-but-off, structured logging NOT
IMPLEMENTED).

---

## 17. MCP protocol surface (LLD stack; build prompt §8, §29)

**Sutr today.** Gateway: StreamableHTTP only (`mcp/asgi.py`, `mcp/server.py`), stateful, session-id
+ SSE notifications *within* StreamableHTTP. Outbound client: StreamableHTTP only. Generated
servers: `--transport stdio|http`.

**Delta.** A standalone SSE gateway transport, stdio where appropriate, plain SSE in generated
servers, and protocol integration tests against the official reference clients.

**Status: PARTIAL.**

---

## 18. Multi-tenancy (build prompt §61; LLD principle "Multi-Tenant")

**Sutr today.** `User ⇄ OrgMembership ⇄ Org` with workspaces and a role matrix (`0023` migration).
Org scoping is applied per query in each router.

**Delta.** The LLD's tenant is a **provider organization** with isolated identities, secrets, quotas,
billing *and runtime*. Runtime isolation (namespace per provider) does not exist. There is no
systematic negative-test suite proving cross-tenant discovery/runtime/secret/log/billing/execution
denial — build prompt §61 requires explicit negative tests.

**Status: PARTIAL.**

---

## 19. API standards & contracts (LLD §5.5)

**LLD requirement.** REST/JSON/HTTPS/stateless/OpenAPI 3.1, `/v1/...`, standard envelope, cursor
pagination, `X-Request-ID`/`X-Correlation-ID`/`Idempotency-Key`, per-tier rate limits, contracts in
`contracts/openapi/*.yaml` validated in CI, Pact contract tests.

**Sutr today.** REST/JSON under `/api/...` (**not** `/v1/...`), FastAPI's generated OpenAPI at
`/docs`, no standard envelope, offset pagination where paginated, no idempotency keys, no
`contracts/` directory, no contract tests.

**Status: PARTIAL.** Note the backward-compatibility constraint: `/api/...` is consumed by the
published `sutr-cli` on npm and by both SDKs. New surfaces get `/v1/...`; existing ones keep `/api`
and are aliased, never moved.

---

## 20. DevSecOps (LLD §5.7)

**LLD requirement.** dependency scan, secret detection, SAST (Semgrep), container scan (Trivy),
license check, SBOM (Syft), signing (Cosign); build fails on critical vulnerabilities; release gate =
green CI + security approval + policy validation + signed artifacts + audit log; DORA metrics.

**Sutr today.** Four workflows: backend lint (ruff), frontend lint (prettier), tests (server + both
SDKs), fly deploy gated on pytest.

**Delta.** No CLI build/test job, no SDK publishing, no CodeQL, no dependency audit, no Semgrep, no
Gitleaks, no Trivy, no SBOM, no Cosign, no failing gate on critical findings.

**Status: PARTIAL.**

---

## 21. Resilience, load, chaos (LLD §5.8; build prompt §58–§60, §67–§69)

**Sutr today.** Per-call timeouts, one OAuth refresh-retry, response size caps, in-memory rate
limiters. No circuit breaker, no bulkheads, no health scoring, no quarantine, no fallback tiers, no
k6 load tests, no chaos tests. The LLD's performance targets (100k invocations/min, 10k concurrent
agents, 1M searches/day, 99.95% availability) are **unmeasured** and must not be claimed.

**Status: PARTIAL** (timeouts/retry IMPLEMENTED); performance targets **NOT TESTED**.

---

## 22. Blocked items — external documentation required

| Item | Why blocked | What unblocks it |
|---|---|---|
| **Swaraj Cloud provider** | No public API documentation exists in the repo or in the LLD; the name appears only in comments. Build prompt §4/§7 forbid invention. | Official Swaraj Cloud API documentation (auth model, endpoints, request/response schemas, region model). |
| **ESDS internal APIs** (billing, identity federation) | Not specified anywhere in the LLD. | ESDS internal API specs. |
| **Live cloud provider verification** (GCP/Azure/AWS) | Implemented against documented REST APIs but never run against live paid accounts. | Paid accounts + a verification run. Until then the status stays **NOT TESTED**, not "working". |
| **Apicurio/Avro schema registry** | Requires a running registry; contract shape is documented, wiring is not. | An Apicurio endpoint in the target environment. |

---

## 23. Phase plan derived from this gap analysis

Ordered by dependency, not by LLD chapter order. Each phase ends with the full suite green and the
traceability matrix updated.

| Phase | Content | Depends on |
|---|---|---|
| **0** | This audit + `REFERENCE_MAP.md` + `ARCHITECTURE_DECISIONS.md` + `ESDS_TRACEABILITY_MATRIX.md` | — |
| **1** | Complete Sutr's partial features: MCP SSE/stdio, structured JSON logging, Spectral-style linting, Swagger 2.0 conversion, full security translation, non-JSON bodies, unified runtime mode, generated-server SSE, deployment update/rollback, deployment metrics, marketplace, SDK publishing, quota enforcement, CI security | 0 |
| **2** | Foundation: service boundaries, shared contracts, event envelope + outbox + bus abstraction, correlation IDs end to end, idempotency keys, standard error envelope, `/v1` versioning, DB ownership map | 1 |
| **3** | Source connectors (7 verbs), continuous sync, drift detection + classification, IR versioning and diagnostics | 2 |
| **4** | Documentation intelligence: ingestion, parsing, OCR, chunking, business rules, workflows, glossary, embeddings, graph | 2 |
| **5** | MCP generation + runtime: immutable artifacts, SBOM, scanning, generated-server auth, Kubernetes provider, runtime manager, update/rollback, GitOps | 2 |
| **6** | Registry + marketplace: immutable versions, lifecycle, trust score, subscriptions, provider profiles, pricing metadata | 3,5 |
| **7** | Discovery: lexical + vector + graph hybrid, policy pre-filter, ranking, fallbacks, cache | 4,6 |
| **8** | Provisioning + zero trust: agent identity, scoped access passes, PDP/PEP, tenant isolation tests, Vault, runtime identity | 5 |
| **9** | Governance: policy engine, compliance, risk, review workflow, exceptions, audit expansion | 8 |
| **10** | Billing: quotas (moved earlier into Phase 1 for enforcement), pricing, invoices, settlement, revenue share, dunning | 2 |
| **11** | Observability: OTel end to end, Grafana/Loki/Tempo/Alertmanager assets, tenant-isolated telemetry | 2 |
| **12** | Enterprise infrastructure: HA compose/Helm for Postgres, Kafka, Redis, OpenSearch, Qdrant, Neo4j, MinIO, Vault; multi-region foundations | 11 |
| **13** | DevSecOps: SAST, dependency/secret/container scanning, SBOM, signing, contract tests, load tests, E2E, chaos | all |

---

## 24. Non-negotiables carried into every phase

1. **The 953 existing tests stay green.** A phase is not done if it regresses one.
2. **`/api/...` keeps working.** The published CLI and both SDKs depend on it.
3. **No invented external APIs.** Anything undocumented is marked BLOCKED, not guessed.
4. **No fake completion vocabulary.** "Production-ready", "HA", "multi-region", "99.95%",
   "100k/min" are forbidden until measured against the corresponding requirement.
5. **Every new capability ships with tests**, including its failure behaviour.
6. **Every architectural decision is recorded** in `ARCHITECTURE_DECISIONS.md`.
