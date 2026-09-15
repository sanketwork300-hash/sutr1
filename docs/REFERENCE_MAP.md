# Reference Map — open-source projects consulted per feature

**Phase 0 deliverable.** Build prompt §2 (*Reference Lock*) requires that, before implementing any
major feature, the relevant open-source reference project is inspected, and that the reference is
treated as an **architectural and behavioural reference — not automatically a dependency**.

This file is the register. It records, per feature: what the ESDS LLD asks for, what Sutr already
has, which reference project informs the design, which component of it is relevant, what behaviour is
adopted, and — critically — **whether the project becomes a runtime dependency, and why**.

Rules that hold for every row:
- Source code is **not** copied from reference projects.
- A reference becomes a dependency only when there is a stated technical requirement.
- Where Sutr's existing implementation already satisfies the requirement, the reference is used to
  *validate* the design, not to replace it.

---

## Dependency verdicts at a glance

| Reference | Dependency? | Why |
|---|---|---|
| MCP specification + `mcp` Python SDK | **Yes — already** | Sutr speaks MCP; the SDK is the protocol implementation |
| `openapi-spec-validator` (Python) | **Yes — already** | Official meta-schema validation |
| Prometheus client, OpenTelemetry SDK | **Yes — already** | Instrumentation libraries, not services |
| Kafka client (`confluent-kafka`/`aiokafka`) | **Optional backend** | ADR-003: bus abstraction, Kafka is one implementation |
| Qdrant client, Neo4j driver, OpenSearch client | **Optional backends** | ADR-015: every one degrades to a working default |
| HashiCorp Vault client | **Optional backend** | ADR-015: falls back to existing `secrets/` KMS backend |
| Keycloak, OpenFGA, Casbin, OPA | **No** | Behavioural references only — Sutr owns its identity and authz |
| Composio | **No** | Explicit build-prompt instruction; concepts only |
| Spectral, OpenAPI Generator, openapi-diff, swagger-parser | **No** | Node/Java toolchains; rules and mappings reimplemented natively (ADR-013, ADR-014) |
| Unstructured, Tesseract, spaCy | **Optional** | Documentation intelligence degrades without them |
| Temporal, Celery | **No (initially)** | The outbox + stage runner covers resumability; revisit if it does not |
| Trivy, Semgrep, Gitleaks, Syft, Cosign | **CI tools** | Pipeline steps, not runtime dependencies |
| Kubernetes, Argo CD, Terraform, Kyverno, Istio, Kong/Envoy | **Deployment targets** | Manifests and providers, not libraries |
| Pact, k6, Playwright, LitmusChaos | **Test tooling** | |

---

## Identity & authentication

**LLD requirement (§4.3).** OAuth2, OIDC, JWT, MFA, sessions, token lifecycle, client registration.

**Sutr today.** `api/user_auth.py`, `api/totp.py` (real TOTP + recovery codes), `api/google_login.py`,
`mcp/oauth_provider.py` + `api/oauth_server.py` (a full OAuth 2.1 authorization server with Dynamic
Client Registration, PKCE, refresh rotation, revocation, consent UI), `auth_tokens.py`.

**Reference.** `keycloak/keycloak` — token lifecycle, refresh rotation semantics, DCR, session and
MFA models.

**Behaviour adopted.** Refresh-token rotation with revocation of the superseded token; PKCE required
on every authorization-code flow; DCR client metadata validation. Sutr already implements all three;
Keycloak was consulted to confirm the rotation and revocation semantics are the standard ones.

**Sutr-specific adaptation.** Sutr is both an OAuth *client* (toward integrations) and an OAuth
*server* (toward MCP clients). Keycloak is a server only, so the client half is Sutr's own design in
`auth_start.py` and `mcp/oauth.py`.

**Dependency:** No. **Status:** IMPLEMENTED.

---

## Authorization, PDP/PEP, policy as code

**LLD requirement (§4.3, §5.2).** Four authorization layers evaluated in order; ABAC example
`Finance ∧ Organization=Bank-A ∧ Region=India ⇒ Allow Refund Tool`; PDP decides centrally, PEPs
enforce everywhere; declarative versioned policies with a lifecycle; evaluation <50 ms cached.

**Sutr today.** `authz.py` — a 5-role × 11-permission matrix. `approvals/policy.py` — per-tool
`allow | require_approval | deny`, deny-by-default, evaluated inline in the execution pipeline.

**References.**
- `open-policy-agent/opa` — PDP architecture, decision logging, policy bundles, the
  input-document/decision-document contract.
- `casbin/casbin` — the separation of a *model* (what dimensions exist) from *policy* (the rules),
  and RBAC-with-domains as the shape that matches org-scoped roles.
- `openfga/openfga` — relationship tuples. **Reviewed and deliberately not adopted** (ADR-005):
  Sutr's resource graph is flat.

**Behaviour adopted.** OPA's PDP/PEP split and its request/decision contract shape: a PEP submits a
structured input document (subject, action, resource, context) and receives a decision plus a
*reason*, and every decision is logged. Casbin's model/policy separation informs how ABAC conditions
are declared apart from the role matrix.

**Sutr-specific adaptation.** The PDP is an in-process service with the same interface an out-of-
process OPA would expose, so moving to a sidecar later is a transport change. Rego is not adopted:
introducing a second policy language into a Python codebase for a fixed, well-known set of
dimensions costs more than it returns.

**Dependency:** No. **Status:** PARTIAL (invocation-time enforcement only).

---

## MCP protocol, transports, and gateway

**LLD requirement.** MCP-native tools; framework-agnostic agent access.

**Sutr today.** `mcp/server.py` + `mcp/asgi.py` — stateful StreamableHTTP gateway with 11 `sutr__`
meta-tools; `mcp/client.py` — outbound StreamableHTTP client; generated servers support
`--transport stdio|http`.

**References.**
- `modelcontextprotocol/modelcontextprotocol` — the specification itself: tool lifecycle, transport
  semantics, authorization, session handling. This is the authority for anything protocol-shaped.
- `modelcontextprotocol/servers` — reference server implementations, used to check tool-schema shape
  and client compatibility.

**Behaviour adopted.** StreamableHTTP session semantics (session id header, SSE notification stream)
are already implemented per spec. The SSE transport to be added follows the specification's SSE
transport, not an invented variant.

**Sutr-specific adaptation.** Sutr's gateway addresses upstream tools by `(integration_id,
tool_name)` and exposes management meta-tools — an aggregation layer the reference servers do not
have.

**Dependency:** Yes (`mcp` Python SDK, already present). **Status:** PARTIAL (transports).

---

## Tool registry, connected accounts, tool execution

**LLD requirement (§3.7, §3.8).** Registry, marketplace, discovery, connected accounts, agent-facing
abstractions.

**Sutr today.** `integrations/registry.py`, `connections/` (OAuth grants scoped to
`(org, user, provider)` with tokens in `secret` rows and refresh centralised in
`connections.store.access_token()`), `services/tool_catalog.py`, `services/tool_pipeline.py`.

**Reference.** `ComposioHQ/composio` — tool registry structure, connected-account model, tool
discovery surface, provider integration patterns, agent-facing abstractions.

**Behaviour adopted.** The connected-account concept (a durable, revocable, per-principal grant that
tools consume without ever seeing the credential) — which Sutr already implements, with a stricter
rule than most: a connection is a *personal* grant, an API key cannot open one, and no caller decides
whether to refresh.

**Explicitly not adopted.** Composio's code, its hosted tool registry, and any dependency on it —
the build prompt names this exclusion directly.

**Dependency:** No. **Status:** IMPLEMENTED (connected accounts); NOT IMPLEMENTED (registry as a
system of record).

---

## OpenAPI parsing, $ref resolution, validation

**LLD requirement (§3.4).** Parse heterogeneous specs; `$ref` resolution; circular refs rejected with
diagnostics; 10,000+ operations; deterministic IR.

**Sutr today.** `openapi/loader.py`, `openapi/resolver.py` (DFS with on-path cycle placeholders,
external `$ref` refused, depth and node budgets), `openapi/normalizer.py`.

**References.**
- `APIDevTools/swagger-parser` — `$ref` resolution strategy, dereferencing vs bundling, validation
  ordering.
- `OpenAPITools/openapi-generator` — the operation model that survives contact with real specs
  (path-item parameter inheritance, media-type selection, security requirement resolution).

**Behaviour adopted.** Resolve-then-validate ordering; treat a circular `$ref` as a bounded
placeholder rather than an error where the cycle is benign; refuse external `$ref` outright (a
stricter choice than swagger-parser's, because a spec URL is untrusted input and following external
refs is an SSRF primitive).

**Dependency:** No (Python `openapi-spec-validator` is used for meta-schema validation).
**Status:** IMPLEMENTED.

---

## OpenAPI linting

**LLD/build-prompt requirement (§16).** Rule ID, severity, message, location, JSON pointer,
documentation, remediation; all findings, not the first.

**Sutr today.** `openapi_spec_validator` surfaces the first structural error only.

**Reference.** `stoplightio/spectral` — rule identity (`oas3-*`, `operation-*` naming), the
`error|warn|info|hint` severity ladder, and the diagnostic shape (`code`, `message`, `path`, `range`,
`severity`).

**Behaviour adopted.** Stable rule IDs matching Spectral's names where the same check exists, so a
finding is recognisable to anyone who has used Spectral; severity semantics; all findings returned.

**Sutr-specific adaptation.** Reimplemented natively in Python (ADR-013) — Spectral is a Node
toolchain and the LLD's backend stack is Python. Severities collapse to `ERROR|WARNING|INFO` per the
build prompt. Every rule carries remediation text, which Spectral does not provide.

**Dependency:** No. **Status:** NOT IMPLEMENTED → Phase 1.

---

## Swagger 2.0 conversion

**Build-prompt requirement (§17).** Convert 2.0 → 3.x preserving paths, parameters, definitions,
security, consumes, produces, responses; actionable errors.

**References.** `OpenAPITools/openapi-generator` (its Swagger 2 compatibility layer) and
`APIDevTools/swagger-parser` (its converter) — both for the *mapping rules*, which are a matter of
record, not invention.

**Behaviour adopted.** The standard mapping: `host`+`basePath`+`schemes` → `servers`; `definitions`
→ `components.schemas` with `$ref` rewriting; `body` parameter → `requestBody` using `consumes` for
media types; `formData` → form-encoded request body; `produces` → per-response `content`;
`securityDefinitions` → `components.securitySchemes` (`basic` → `http`/`basic`, OAuth2 flow
renaming); `file` type → `string`/`binary`.

**Dependency:** No. **Status:** NOT IMPLEMENTED → Phase 1.

---

## Drift detection / API diffing

**LLD requirement (§3.3).** Compare a second source against the source of truth and report drift;
classify changes; breaking changes must not auto-deploy.

**Reference.** `OpenAPITools/openapi-diff` — the breaking vs non-breaking taxonomy (removed
endpoint, removed/narrowed parameter, changed required-ness, changed response schema, changed
security).

**Behaviour adopted.** The classification taxonomy, extended with the build prompt's additional
categories `SECURITY`, `DOCUMENTATION`, `METADATA`.

**Sutr-specific adaptation.** Diffing runs over the **canonical IR**, not the raw documents, so drift
is reported in terms of tools and operations rather than YAML lines.

**Dependency:** No. **Status:** NOT IMPLEMENTED → Phase 3.

---

## OpenAPI → MCP generation

**LLD requirement (§3.6).** IR + documentation knowledge + runtime template → validated MCP server;
Python, Go, Node.js templates; deterministic builds.

**Sutr today.** `openapi/compiler.py` (IR → declarative tools) and `openapi/packaging.py`
(deterministic 9-file Python package with a zero-dependency runtime).

**References.**
- `cnoe-io/openapi-mcp-codegen` — the IR → operation → tool mapping and generation structure.
- `harsha-iiiv/openapi-mcp-generator` — operation-to-tool conversion and tool-naming conventions.
- `pvliesdonk/openapi-mcp` — generic OpenAPI → HTTP → MCP execution at runtime (the *hosted proxy*
  model, which is what Sutr's gateway does).
- `awslabs/mcp` — provider integration patterns and tool selection at scale.

**Behaviour adopted.** The two-model split these projects collectively demonstrate — *generate a
server* vs *proxy dynamically* — is exactly Sutr's `STANDALONE` vs `HOSTED` distinction (ADR-010).
Tool naming from `operationId` with deterministic fallback is the common convention; Sutr's
three-step collision handling that **fails loudly** rather than truncating is stricter than any of
them, and is kept.

**Dependency:** No. **Status:** PARTIAL (Python template only).

---

## Documentation intelligence

**LLD requirement (§3.5).** PDF/HTML/DOCX/MD/TXT parsing, OCR fallback, semantic chunking, parallel
extraction of business rules, workflows, glossary, knowledge graph, embeddings.

**Sutr today.** Nothing.

**References.** `Unstructured-IO/unstructured` (document partitioning into semantic elements),
`tesseract-ocr/tesseract` (OCR fallback for scanned pages), `explosion/spaCy` (NER and linguistic
processing for glossary and entity extraction).

**Behaviour adopted.** Unstructured's partition-then-chunk model — partition into typed elements
(title, narrative, table, list) and chunk by title/section so a chunk is semantically whole, rather
than fixed-size windows that cut rules in half. Tesseract as a *fallback for pages that produced no
text*, with failed pages flagged and the rest continuing (LLD §3.5 failure rule).

**Sutr-specific adaptation.** All three are optional (ADR-015): without them, plain-text and Markdown
ingestion still work and the pipeline reports reduced capability rather than failing.

**Dependency:** Optional. **Status:** NOT IMPLEMENTED → Phase 4.

---

## Knowledge graph

**LLD requirement (§3.5, §5.4).** Neo4j; entities Provider/Customer/Account/Payment/Refund/API/Tool/
Workflow/BusinessRule/Role/Region/ComplianceRequirement; edges OWNS/CONTAINS/ELIGIBLE_FOR/IMPLEMENTS/
REQUIRES/MAY_INVOKE/CONTAINS_OPERATION.

**Reference.** `neo4j/neo4j` — property-graph modelling, Cypher query patterns, relationship
direction conventions.

**Behaviour adopted.** Property-graph modelling with typed relationships; graph used for *expansion*
during retrieval (find tools related to entities in the intent), never as the canonical store — the
LLD is explicit that the graph enriches and must never replace the IR.

**Dependency:** Optional. **Status:** NOT IMPLEMENTED → Phase 4.

---

## Vector search and lexical search

**LLD requirement (§3.8, §5.4).** Qdrant/Milvus/pgvector for semantic search; OpenSearch for lexical;
metadata filtering; tenant/version/region filters; hybrid retrieval.

**References.** `qdrant/qdrant` (collections, payload filtering, named vectors) and
`opensearch-project/OpenSearch` (indexing, analyzers, filtering, ranking).

**Behaviour adopted.** Qdrant's *filter-then-search* model — the policy filter is expressed as a
payload filter applied during retrieval, which is what makes the LLD's "policy filter before ranking"
requirement efficient rather than a post-hoc discard. OpenSearch's analyzer/field-boost model for
exact-identifier matching.

**Sutr-specific adaptation.** Three-tier fallback (Qdrant → pgvector → lexical-only), and embedding
generation is asynchronous so discovery never blocks on it.

**Dependency:** Optional backends. **Status:** NOT IMPLEMENTED → Phase 7.

---

## Events and schema registry

**LLD requirement (§5.6).** Kafka topics, partitions, consumer groups, at-least-once, ordering per
partition, Apicurio + Avro, per-domain retry → DLQ.

**References.** `apache/kafka` (delivery semantics, partition ordering, consumer-group rebalancing,
retry topics) and `Apicurio/apicurio-registry` (schema compatibility levels, subject naming).

**Behaviour adopted.** Partition key per entity for ordering; consumer-group-per-service for
independent pacing; retry topic → DLQ per domain; backward-compatible schema evolution with
`event_version` in the envelope.

**Sutr-specific adaptation.** ADR-003 — the transactional outbox is the producer contract, and Kafka
is a pluggable backend so the self-hosted single-container install keeps working.

**Dependency:** Optional backend. **Status:** NOT IMPLEMENTED → Phase 2.

---

## Durable workflows

**LLD requirement (§4.1).** Asynchronous, event-driven, resumable pipeline; failures resume from the
last good stage.

**References.** `temporalio/temporal` (durable execution, checkpointing, retry policies, resume
semantics) and `celery/celery` (worker pools, task routing, per-queue isolation — the LLD's
"bulkheads").

**Behaviour adopted.** Temporal's *checkpoint-per-stage* discipline: persist stage state before and
after every step, so a resume needs no replay of side effects. Celery's isolated queues per workload
map onto the LLD's bulkhead requirement (runtime workers, translation jobs, doc processing, billing
workers).

**Sutr-specific adaptation.** Neither becomes a dependency initially. The pipeline is linear and its
stages are already persisted entities; a stage runner over the outbox provides checkpointing and
resume without a workflow engine. If branching or long-running human-in-the-loop timers appear,
this decision is revisited and recorded.

**Dependency:** No (initially). **Status:** NOT IMPLEMENTED → Phase 2.

---

## Secrets

**LLD requirement (§4.3, §5.4).** Vault + KMS; runtime → secret reference → Vault → temporary
credential → provider API; rotation, versioning, audit, per-tenant isolation; credentials never in
generated code, env vars, or databases.

**Sutr today.** `secrets/` with `db` (plaintext, default) and `db_kms` (AES-256-GCM envelope)
backends; `deploy/credentials.py` resolves cloud credentials fresh per operation, never caching them
on the deployment row.

**Reference.** `hashicorp/vault` — secret references vs values, dynamic credentials, leases and
renewal, the audit device model.

**Behaviour adopted.** The *reference not value* discipline, which Sutr already follows on the
`provider_connection` row. Leases/dynamic credentials map onto the scoped-access-pass model
(ADR-007).

**Dependency:** Optional backend. **Status:** PARTIAL (plaintext default is the gap).

---

## Caching and rate limiting

**Reference.** `redis/redis` — TTL semantics, distributed counters, sliding-window rate limiting.

**Behaviour adopted.** Sliding-window counters for quotas and concurrency; cache invalidation on
tool publish/update, policy change, and runtime unavailability (LLD §3.8's exact invalidation list).

**Sutr today.** `rate_limit.py` is in-memory and single-process — honest for one worker, wrong for
several. This is the stated caveat, not a hidden one.

**Dependency:** Optional backend. **Status:** PARTIAL.

---

## Object storage

**Reference.** `minio/minio` — S3-compatible API surface, bucket/prefix layout for artifacts.

**Behaviour adopted.** S3 API compatibility as the interface, so MinIO locally and any S3-compatible
store in production are the same code path. Artifacts stored under
`{tenant}/{provider}/{tool}/{version}/` so retention and residency policies can be applied by prefix.

**Dependency:** Optional. **Status:** NOT IMPLEMENTED → Phase 5.

---

## Observability

**References.** `open-telemetry/opentelemetry-collector` (pipeline model: receivers → processors →
exporters), `prometheus/prometheus`, `grafana/grafana`, `grafana/loki`, `grafana/tempo`.

**Behaviour adopted.** The collector's processor stage is where PII masking and tenant-label
stripping belong — a platform property, not something each service must remember. Loki's label
discipline (few, low-cardinality labels; everything else in the log line) matches the existing
metrics decision in `observability/metrics.py`.

**Sutr today.** Prometheus metrics with a deliberate no-tenant-label policy; OTel spans available
and off by default.

**Dependency:** Yes (client libraries, already present). **Status:** PARTIAL.

---

## Kubernetes, GitOps, infrastructure

**References.** `kubernetes/kubernetes` (Deployment, Service, Namespace, ServiceAccount, Secret,
ConfigMap, HPA, NetworkPolicy), `argoproj/argo-cd` (declarative reconciliation, drift detection,
sync waves, rollback), `hashicorp/terraform` (provisioning), `kyverno/kyverno` and
`open-policy-agent/gatekeeper` (admission policy).

**Behaviour adopted.** Argo's *desired state in Git, reconciliation by the controller* model —
Runtime Manager writes manifests to a Git repository rather than calling the Kubernetes API
directly, which is what makes rollback and drift detection real rather than bookkeeping.
Namespace-per-provider with NetworkPolicy and a dedicated ServiceAccount is the LLD's runtime
isolation requirement expressed in Kubernetes primitives.

**Dependency:** Deployment targets. **Status:** NOT IMPLEMENTED → Phase 5.

---

## Networking and gateway

**References.** `Kong/kong`, `envoyproxy/gateway`, `istio/istio`, `nginx/nginx` — routing, mTLS,
rate limiting, service mesh identity.

**Behaviour adopted.** Istio's workload-identity model (SPIFFE-style service identity, mTLS by
default) is the reference for "every service and runtime has an identity". Gateway-level rate
limiting sits in front of application quotas — two different concerns at two different layers, and
the LLD has both.

**Sutr today.** `Caddyfile` for TLS termination and reverse proxy in the self-hosted deployment.

**Dependency:** Deployment targets. **Status:** NOT IMPLEMENTED.

---

## Supply-chain security

**References.** `semgrep/semgrep` (SAST), `aquasecurity/trivy` (dependency + container scanning),
`gitleaks/gitleaks` (secret detection), `anchore/syft` (SBOM), `sigstore/cosign` (signing).

**Behaviour adopted.** The LLD's exact gate order — dependency scan → secret detection → SAST →
container scan → license check → SBOM → sign — with the build failing on critical findings. SBOM
generated per build and stored **with the artifact**, because an SBOM that is not attached to the
thing it describes is not evidence.

**Dependency:** CI tools. **Status:** NOT IMPLEMENTED → Phase 1 (CI) / Phase 5 (artifacts).

---

## Testing

**References.** `pact-foundation/pact-js` (consumer-driven contract testing), `grafana/k6` (load),
`microsoft/playwright` (frontend E2E), `litmuschaos/litmus` (chaos).

**Behaviour adopted.** Pact's consumer-driven direction: the SDKs and CLI are the consumers, and
their expectations become the contract the server must satisfy — which is the right direction here,
because those consumers are already published and their compatibility is the constraint. k6
scenarios per subsystem so a Discovery spike can be shown not to starve Billing or the Gateway (LLD
§5.8 / build prompt §68).

**Dependency:** Test tooling. **Status:** NOT IMPLEMENTED → Phase 13.

---

## Design-record template

Every major feature added from Phase 1 onward appends a record in this form (build prompt §2):

```
Feature:
ESDS LLD requirement:
Existing Sutr implementation:
Reference repository:
Relevant reference component:
Behavior adopted:
Sutr-specific adaptation:
Dependencies:
Database changes:
API changes:
Events:
Tests:
Known limitations:
```
