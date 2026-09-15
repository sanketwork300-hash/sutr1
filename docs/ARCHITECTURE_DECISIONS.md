# Architecture Decision Records — Sutr → ESDS Sovereign Agent Platform

Every decision taken while evolving Sutr into the ESDS Sovereign Agent Platform is recorded here,
in the order it was taken. The build prompt's rule is the reason this file exists: *"If the ESDS LLD
requires something but neither the existing code nor the references specify implementation details,
document the implementation decision explicitly. Never silently invent behavior."*

**Source priority** used throughout (build prompt §1):
1. Existing working Sutr functionality
2. ESDS Sovereign Agent Platform LLD (Condensed v1.1)
3. Official protocol/specification documentation
4. Open-source reference repositories (see `REFERENCE_MAP.md`)
5. A new decision — recorded here, never assumed

Record format: **Context → Decision → Consequences → Reversibility**.

---

## ADR-001 — Modular monolith before microservices

**Status:** Accepted (Phase 0)

**Context.** The LLD §2.3 names 15 control-plane services, §2.4 mandates *"database-per-service (no
exceptions)"*, and §2.6 describes one Git repository holding all of them with identical Clean
Architecture layouts. Sutr is one FastAPI process with one SQLModel database. The build prompt is
explicit in the other direction: *"Initially these may remain inside the Sutr monorepo as modular
services. DO NOT prematurely split them into 15 deployments. Create clear package/service boundaries
first."*

**Decision.** Introduce the LLD's 15 services as **Python packages** under `server/src/sutr/`, each
with the LLD's Clean Architecture layers — `api/` (controllers), `service/` (application), `domain/`
(entities and rules), `repository/` (persistence), `models/`, `events/`, `tests/`. Keep one
deployable process. Enforce boundaries by convention plus an import-linting test rather than by
network calls.

**Consequences.**
- Cross-service calls are Python function calls today and can become HTTP/gRPC later without
  changing the caller's *shape*, because callers depend on a service interface, not on a module's
  internals.
- Database-per-service becomes **schema/table ownership** per service: a table is written by exactly
  one service, and other services read it only through that service's repository interface. This is
  the honest local form of the LLD's rule and it is the property that actually makes a later split
  possible.
- The LLD's fault-isolation claim is *not* achieved by this step. That is stated, not implied.

**Reversibility.** High. Extracting a package into its own deployment is mechanical once the import
boundary and the event contracts hold.

---

## ADR-002 — Control-plane / data-plane separation is a dependency rule, not a deployment

**Status:** Accepted (Phase 0)

**Context.** LLD §2.2 requires the data plane to survive control-plane outages: *"New tools blocked;
existing tools keep working."* Sutr runs both in one process against one database.

**Decision.** Define the split as a **directed dependency rule**: data-plane modules (gateway,
runtime router, runtime executor, provider connectors, execution cache, rate limiter) may not import
control-plane modules (provider, source connectors, translation, documentation, metadata, generator,
registry, marketplace, governance authoring, billing aggregation). Everything the hot path needs
from the control plane is read from a cache or a denormalized read model that is refreshed by
events.

**Consequences.**
- The guarantee becomes testable: a test can disable control-plane repositories and assert that an
  already-provisioned tool still executes.
- Some duplication of data is deliberate (the read model). That is the cost of the guarantee.

**Reversibility.** High.

---

## ADR-003 — Event bus abstraction with a durable outbox; Kafka is a backend, not a dependency

**Status:** Accepted (Phase 0)

**Context.** LLD §5.6 mandates Kafka with Apicurio and Avro. Sutr is self-hostable on SQLite with no
broker, and the build prompt requires backward compatibility and preservation of the self-hosted
install path. Reference: `apache/kafka` for topic/partition/consumer-group/delivery semantics,
`Apicurio/apicurio-registry` for schema compatibility.

**Decision.**
- Events are defined once as versioned envelope + payload models in `contracts/events/`.
- Producers write to a **transactional outbox table in the same transaction as the state change** —
  the same discipline `services/audit.py` and `services/metering.py` already use, and the only way
  at-least-once delivery can be honest.
- A relay publishes outbox rows through an `EventBus` protocol. Two implementations: an in-process /
  database-backed bus (default, keeps self-hosting working with zero infrastructure) and a Kafka bus
  (selected by configuration).
- Consumers are idempotent by `event_id`, with a consumed-events table. Retry then DLQ per domain.

**Consequences.**
- Ordering per entity is guaranteed by the Kafka backend via partition key; the in-process backend
  guarantees it per entity by processing an entity's outbox rows in id order. This difference is
  documented, not hidden.
- Avro is deferred: envelopes are JSON with an explicit `event_version`, and the Avro schemas are
  generated from the same contract models when a registry is available. Marked **PARTIAL** until an
  Apicurio endpoint exists.

**Reversibility.** High for the backend; low for the envelope shape, which is why the envelope is
fixed from the LLD's required field list before anything is emitted.

---

## ADR-004 — `/api/...` is preserved; `/v1/...` is the new surface

**Status:** Accepted (Phase 0)

**Context.** LLD §5.5 mandates `/v1/...` with a standard envelope and cursor pagination. Sutr serves
`/api/...` with bare JSON bodies, and that surface is consumed by `sutr-cli` **already published on
npm** and by both SDKs. Build prompt §8 requires backward compatibility.

**Decision.** Existing `/api/...` routes keep their paths, shapes, and semantics indefinitely. New
platform services are mounted under `/v1/...` with the LLD's envelope, cursor pagination, and
required headers. Where a `/v1` route supersedes an `/api` route, the `/api` route stays as a thin
adapter over the same service layer — never a second implementation.

**Consequences.** Two envelope styles coexist. This is a deliberate compatibility cost and is
documented in the API reference rather than resolved by breaking clients.

**Reversibility.** Low by design — that is the point of a compatibility promise.

---

## ADR-005 — Role matrix stays; ReBAC is not adopted

**Status:** Accepted (carried forward from the Sutr baseline, re-confirmed in Phase 0)

**Context.** LLD §4.3 describes four authorization layers and an ABAC example
(`Finance ∧ Organization=Bank-A ∧ Region=India ⇒ Allow Refund Tool`). References `openfga/openfga`
(relationship tuples) and `casbin/casbin` (RBAC/ABAC models) were reviewed.

**Decision.** Keep `authz.py`'s 5-role × 11-permission matrix as the RBAC layer. Add ABAC as a
**separate attribute-condition layer** evaluated by the PDP, not by rewriting RBAC into relationship
tuples. Sutr's resource graph is flat (org → workspace → integration → tool); ReBAC's value appears
with deep, arbitrary ownership graphs that Sutr does not have.

**Consequences.** ABAC conditions are declarative and versioned; RBAC stays a fast in-memory matrix
check. If a future requirement introduces nested resource ownership, this ADR is superseded rather
than patched.

**Reversibility.** Medium.

---

## ADR-006 — Swaraj Cloud ships as an interface with a `DOCUMENTATION_REQUIRED` state

**Status:** Accepted (Phase 0)

**Context.** Build prompt §4 and §7 are unambiguous: the Swaraj Cloud adapter is not implemented,
its name exists only in comments, and its API must not be invented. No documentation for it exists
in the repository or the LLD.

**Decision.** Implement `SwarajCloudProvider` against the existing `DeploymentProvider` ABC with
every method present. `available()` returns `(False, "…")` with a reason naming the missing
documentation; every other method raises `ProviderError` carrying a
`NOT_CONFIGURED / DOCUMENTATION_REQUIRED` code. The provider is registered so it is visible and
honestly labelled in the UI, and a test pins the refusal so no future change can quietly turn it
into a guess.

**Consequences.** Users see the provider and see exactly why it cannot be used. The traceability
matrix carries **BLOCKED — EXTERNAL API DOCUMENTATION REQUIRED** for this row until real docs arrive.

**Reversibility.** High — the interface is the contract.

---

## ADR-007 — Generated servers get an explicit governance mode, not implied inheritance

**Status:** Accepted (Phase 0)

**Context.** Build prompt §40: Sutr's gateway enforces approvals; a standalone generated server
running on someone else's infrastructure does not, and *"Do NOT silently pretend standalone servers
inherit Sutr governance."*

**Decision.** Generated packages take `GOVERNANCE_MODE`:
- `standalone` (default) — the README and startup log state plainly that this deployment is
  independent of the platform's approval policies.
- `platform` — the server validates a signed access pass (issuer, audience, expiry, nonce, tenant,
  agent, tool, scope) before executing any tool, and refuses to start if the validation material is
  not configured.

**Consequences.** The dishonest middle ground — a governance-looking flag that does nothing — is
excluded by construction. `platform` mode depends on Provisioning (Phase 8), so until then the mode
exists, is documented, and refuses to start rather than pretending.

**Reversibility.** High.

---

## ADR-008 — Non-JSON request bodies are compiled, never silently dropped

**Status:** Accepted (Phase 0, implemented in Phase 1)

**Context.** `openapi/normalizer.py` picks a JSON media type and emits an `unsupported_body` warning
for everything else, so a form-encoded or multipart operation compiles into a tool that posts
nothing. Build prompt §26 requires `application/json`, `application/*+json`,
`application/x-www-form-urlencoded`, `multipart/form-data`, `application/octet-stream`, and binary,
with the content type preserved.

**Decision.** The IR's body node carries the selected content type and an encoding kind
(`json | form | multipart | binary | text`). The compiler maps each kind to parameters, and both
runtimes (hosted `api_client.dispatch_api_tool` and the generated `sutr_runtime.py`) branch on the
kind so **the same request is produced by both**. Binary bodies take base64-encoded string input,
because an MCP tool argument is JSON and there is no other honest way to carry bytes; this is stated
in the tool description.

**Consequences.** Media-type selection becomes a documented preference order rather than
"first JSON wins". Operations that previously compiled to a body-less tool now compile correctly,
which changes generated request shapes — an improvement, but a behaviour change, so it is covered by
tests on both runtimes.

**Reversibility.** Medium.

---

## ADR-009 — OAuth2 in imported specs is a real grant, not a pasted bearer token

**Status:** Accepted (Phase 0, implemented in Phase 1)

**Context.** `openapi/security.py` maps `oauth2` and `openIdConnect` to "paste an access token".
Build prompt §24: *"Do not silently degrade OAuth2 into pasted bearer tokens."*

**Decision.** Model the spec's declared flows. **Client credentials** is executed by the platform:
client id/secret are stored as secret references, the token endpoint is called, the token is cached
until expiry, and it is refreshed on demand — Sutr already owns every piece of this machinery in
`connections/` and `mcp/oauth.py`. **Authorization code** reuses the existing connected-accounts
flow. A pasted token remains available as an explicit, labelled fallback the user chooses, not a
silent downgrade. Multiple schemes may be active simultaneously; the runtime applies each in its
declared location.

**Consequences.** `AuthTranslation`'s single `token_header`/`token_format` pair is superseded by a
list of credential placements. The old shape is kept as a derived property so existing callers and
tests keep working.

**Reversibility.** Medium.

---

## ADR-010 — Runtime modes unify behind one abstraction; both keep their current behaviour

**Status:** Accepted (Phase 0, implemented in Phase 1)

**Context.** Build prompt §28: the hosted dynamic proxy and the standalone generated server are two
execution models that must not be maintained as incompatible code paths. They already agree — the
generated `sutr_runtime.py` was written to mirror `api_client` exactly — but the agreement is
maintained by hand and only checked by reading.

**Decision.** Extract the request-construction rules (URL building and path-param quoting, query and
header wire names, body assembly per encoding kind, auth placement over param headers) into one
specification with one set of tests, executed against **both** implementations. Expose the mode as
`HOSTED | STANDALONE` on the runtime abstraction.

**Consequences.** Divergence becomes a test failure instead of a support ticket. The generated
package keeps its zero-dependency property — it gets a *copy* generated from the shared source of
truth, not an import of Sutr.

**Reversibility.** High.

---

## ADR-011 — Quotas are enforced before execution and rejected calls are not billed as successes

**Status:** Accepted (Phase 0, implemented in Phase 1)

**Context.** LLD §5.1: *"Quotas (daily/monthly calls, concurrency, tokens, transfer) checked before
execution."* Build prompt §48 adds: return 429, and *"Rejected requests should not be billed as
successful executions."* Sutr has no quota concept.

**Decision.** Quota evaluation runs inside `evaluate_gate`, **before** policy evaluation's expensive
work and always before dispatch, returning a `quota_exceeded` gate result that maps to HTTP 429 with
a `Retry-After`. A rejected call writes a log row with outcome `quota_exceeded` and a usage row with
that same outcome and `quantity=0`, so the rejection is visible and auditable but is not a billable
execution.

**Consequences.** The gate acquires a second reason to refuse. Concurrency quotas require a shared
counter; the in-memory limiter is honest only for a single worker, so the multi-worker story is
Redis and is marked as such rather than assumed.

**Reversibility.** High.

---

## ADR-012 — Structured logging replaces text logging; the LLD's field set is mandatory

**Status:** Accepted (Phase 0, implemented in Phase 1)

**Context.** LLD §5.3 requires every log line to carry correlation ID, agent ID, provider ID, tool
ID, runtime ID, status, duration; build prompt §12 adds timestamp, level, service, tenant ID, region
and forbids logging passwords, API keys, OAuth tokens, secret values, and full sensitive payloads.

**Decision.** A JSON formatter reading a context-variable-backed log context, installed in `main.py`
in place of `basicConfig`'s text format, selectable by configuration so a developer can keep human
output locally. Field population is automatic from request/call context, never hand-passed at call
sites. A redaction filter reuses `services/redaction.py`'s existing rules and is applied to the
formatter, not to individual call sites — a rule enforced by a test that logs a secret-looking value
and asserts it does not appear in the output.

**Consequences.** Log output format changes. That is a deployment-visible change and is called out
in the release notes.

**Reversibility.** High.

---

## ADR-013 — Spectral-style linting is additive; schema validation stays the gate

**Status:** Accepted (Phase 0, implemented in Phase 1)

**Context.** Build prompt §16 requires rule IDs, severities, messages, locations, JSON pointers,
documentation links, remediation, and **all** findings rather than the first. Reference:
`stoplightio/spectral` for rule identity and severity semantics.

**Decision.** A native rule engine in `openapi/linting/` — each rule a small pure function over the
parsed document returning findings with a stable `rule_id` (Spectral's names are used where the same
check exists, so findings are recognisable to anyone who knows Spectral), `severity`
(`ERROR|WARNING|INFO`), JSON pointer, and remediation text. Spectral itself is **not** a dependency:
it is a Node toolchain, and the LLD's stack is Python. Structural validity against the official
meta-schema remains the hard gate; lint findings inform and warn.

**Consequences.** Import responses gain a findings list. `ERROR`-severity lint findings do not block
by default; a governance policy may later choose to block on them, which is exactly the LLD's
"governance metadata" model.

**Reversibility.** High.

---

## ADR-014 — Swagger 2.0 is converted in-process, not by an external converter service

**Status:** Accepted (Phase 0, implemented in Phase 1)

**Context.** Build prompt §17 requires Swagger 2.0 → OpenAPI 3.x conversion preserving paths,
parameters, definitions, security, consumes, produces, and responses, with actionable errors.
`normalizer.py` currently rejects 2.x outright. References: `OpenAPITools/openapi-generator` and
`APIDevTools/swagger-parser` for the mapping rules.

**Decision.** A pure `openapi/swagger2.py` module performing the documented structural mapping
(`definitions → components.schemas`, `body` parameter → `requestBody` with `consumes` as media
types, `formData` → form-encoded body, `produces` → per-response content, `securityDefinitions →
components.securitySchemes` with `basic → http/basic` and OAuth2 flow renaming, `host`+`basePath`+
`schemes` → `servers`), applied before validation so everything downstream sees only 3.x. Anything
the mapping cannot represent produces a conversion diagnostic naming the construct and its location.

**Consequences.** The conversion is a rewrite of the document, so the original is retained as the
provenance artifact and the converted document is what the IR is derived from. Both are recorded.

**Reversibility.** High.

---

## ADR-015 — Optional infrastructure degrades; it never becomes a hard dependency

**Status:** Accepted (Phase 0)

**Context.** LLD §3.8 and §5.4 specify OpenSearch, Qdrant, Neo4j, Redis, MinIO, Vault, Kafka. Sutr's
self-hosted install is a single container with SQLite, and the LLD itself specifies graceful
degradation for each of these.

**Decision.** Every one of these is reached through a narrow interface with a working default that
needs no external service: vector search falls back to pgvector and then to lexical; graph
enrichment is skipped when Neo4j is absent; object storage falls back to the database/filesystem for
small artifacts; Vault falls back to the existing `secrets/` KMS backend; Kafka falls back to the
outbox bus; Redis falls back to the in-process limiter **with the multi-worker caveat stated**.

**Consequences.** Feature availability becomes a runtime property that must be reported honestly —
a `/v1/platform/capabilities` view showing which backends are live and which features are degraded.
Every degradation path gets its own test, per build prompt §79.

**Reversibility.** High.

---

## ADR-016 — Deployment updates are versioned revisions; delete-and-recreate is retired

**Status:** Accepted (Phase 0, implemented in Phase 1)

**Context.** Build prompt §36 forbids implementing update as delete → recreate and requires
versioned deployment artifacts with history and true rollback.

**Decision.** Add `update()` and `rollback()` to `DeploymentProvider`, plus a `deployment_revision`
table recording (revision number, package hash, config snapshot, provider state, created_at,
created_by, outcome). Cloud providers map `update()` onto their native revision mechanisms
(Cloud Run revisions, Container Apps revisions, App Runner deployments); the Docker provider builds
a new image and swaps the container, keeping the previous image tagged so rollback is a re-run of a
retained artifact rather than a rebuild. `rollback()` re-applies a stored revision.

**Consequences.** Storage grows with retained artifacts, bounded by a retention policy. Providers
that cannot express an in-place update must say so via a capability flag rather than silently
falling back to recreate.

**Reversibility.** Medium.

---

## ADR-017 — Performance and availability numbers are measured or absent

**Status:** Accepted (Phase 0)

**Context.** The LLD's cover page and §5.8 carry 99.95% availability, 100k invocations/minute, 10k
concurrent agents, 1M searches/day, sub-second invocation, <500 ms discovery, <200 ms auth. Build
prompt §83 forbids claiming any of them untested.

**Decision.** These are recorded in the traceability matrix as **design targets** with status
**NOT TESTED**. They may only change status when a k6 scenario in `tests/load/` produces a recorded
result, and the result is stored with the environment it was measured on. No documentation prose in
this repository asserts them as achieved.

**Consequences.** Some matrix rows will read NOT TESTED for a long time. That is the correct
reading.

**Reversibility.** None — this is a documentation-integrity rule.


---

## ADR-018 — The marketplace shows nulls where the Registry will be, never zeros

**Status:** Accepted (Phase 1)

**Context.** Build prompt §42 lists trust score, compliance, regions, pricing and versions among
the marketplace's fields. All five are owned by the Registry (LLD §3.7), which is Phase 6 work. The
tempting shortcut is to return `trust_score: 0`, `compliance: []`, `pricing: {}` and move on.

**Decision.** Those fields are returned as `null` / empty **with a `pending_fields` list and a
`pending_reason` naming the Registry**, and the console renders them as "not available yet". A
score of 0 is a statement about the tool; `null` is a statement about the platform, and only the
second one is true today.

**Consequences.** Every listing carries five nulls until Phase 6. That is visible and slightly
awkward, which is the point — it is a standing reminder of unfinished work rather than a silent
lie. The same rule applies to `tool_count`, which is `null` for a remote MCP server whose tools are
only known after a discovery call.

**Reversibility.** High — Phase 6 fills the fields in.

---

## ADR-019 — Marketplace taxonomy lives in one data file, not on 59 integration classes

**Status:** Accepted (Phase 1)

**Context.** The marketplace needs an integration-level category and tags. The obvious place is a
field on each `BundledIntegration` subclass — 59 modules to edit, and 59 more to edit again when
the storefront's taxonomy changes.

**Decision.** `integrations/categories.py` maps integration id → category and tags. An integration
may still declare its own `category`/`tags`, which win. Tags additionally absorb each integration's
own `tool_categories` labels, so a facet like `issues` comes from the integration rather than from
the curated file. An integration missing from the map lands in `Other` — visible and honest —
rather than being keyword-matched into a category it may not belong to.

**Consequences.** A category rename is a one-file change. A test asserts that **no** bundled
integration falls into `Other`, so adding an integration without categorising it fails the build
rather than silently degrading the storefront.

**Reversibility.** High.

---

## ADR-020 — Cloud deployment metrics report what the provider actually exposes

**Status:** Accepted (Phase 1)

**Context.** Build prompt §37 asks for CPU, memory, request count, latency, errors, replicas and
health per deployment. Docker exposes CPU and memory through `docker stats`. Cloud Run, Container
Apps and App Runner expose none of those through their *deployment* APIs — the numbers live in
Cloud Monitoring, Azure Monitor and CloudWatch, each a different API with a different token
audience than the deploy grant covers. This is the same situation the repository already documents
honestly for Azure Container Apps logs.

**Decision.** `metrics()` returns a `ProviderMetrics` in which **every field is optional**. Docker
fills CPU, memory and health. The clouds fill readiness and configured replicas from the resources
they already fetch, and set `unavailable_reason` naming the monitoring API and the scope that would
be required. Nothing is estimated, and a missing number is `null`, never `0`.

**Consequences.** The metrics endpoint is genuinely useful on Docker and partially useful on the
clouds, and says which is which. Wiring the three monitoring APIs is real work with real credential
implications and is deferred rather than faked.

**Reversibility.** High — the shape already has the fields.


---

## ADR-021 — The `/v1` envelope and error shape are ours, and recorded as such

**Status:** Accepted (Phase 2)

**Context.** LLD §5.5 requires a standard response envelope and fixes the HTTP
status contract (400/401/403/404/409/422/429/500/503). The status contract is
legible in the extracted text. The envelope is not: the condensed edition
renders it as a graphic, and the field names inside it do not survive text
extraction — the same problem already recorded for the FR-100…FR-1000 register.

Guessing the field names and attributing them to the LLD would be the exact
failure mode build prompt §4 warns about: presenting an invention as a
specification.

**Decision.** Define the shapes here, explicitly as an implementation decision:

```json
{"data": <payload>, "meta": {"request_id": "...", "correlation_id": "...", "next_cursor": "..."}}
{"error": {"code": "...", "message": "...", "details": {}, "retry_after": 0}, "meta": {...}}
```

`data` and `meta` are always present, even when null or empty — "absent" and
"empty" being different is the kind of subtlety that costs a client an
afternoon. `code` is stable and machine-readable; `message` is for a human and
may be reworded freely, so a client branching on `message` is a client already
broken.

Ask the LLD authors for the intended envelope; if it differs, this is a
pre-release change to a surface with no consumers yet.

**Consequences.** Recorded as an open item (B-8) in the traceability matrix
rather than presented as an LLD requirement met.

**Reversibility.** High while `/v1` has no external consumers; low afterwards.

---

## ADR-022 — Plane separation is enforced by a ratchet, not asserted

**Status:** Accepted (Phase 2)

**Context.** ADR-002 defines the control/data plane split as a dependency rule,
and the LLD §2.2 guarantee is that existing tools keep working when the control
plane is down. Sutr was built as one plane. Writing the import rule as a clean
assertion produced 24 violations on the first run.

There were three options: relax the rule until it passed, classify every
offender as "shared" until it passed, or admit the rule does not hold yet.

**Decision.** The third, mechanically. `boundaries.KNOWN_PLANE_VIOLATIONS`
lists each violating import with the phase that removes it. The test fails on a
*new* violation and also fails on a *stale* entry, so the list is a debt
register that can only shrink. A second test caps its length, so adding to it is
a decision rather than a habit.

Reclassifying the offenders would have made the suite green while making the
architecture diagram a lie — precisely what build prompt §83 forbids.

**Consequences.** Four entries today, each naming Phase 7 or Phase 9. The
guarantee is not claimed anywhere until the list is empty and the failure
behaviour is tested.

**Reversibility.** The mechanism is temporary by design; it deletes itself.

---

## ADR-023 — Idempotency is opt-in per request, and reserves before it works

**Status:** Accepted (Phase 2)

**Context.** Build prompt §76 requires idempotency for billing, payments,
registration, uploads, settlement, token issuance, tool publication and runtime
deployment.

**Decision.** An `Idempotency-Key` header, honoured when sent and ignored when
not, so no existing client changes behaviour. The key is reserved and committed
**before** the work runs, not after: if the reservation happened afterwards, two
concurrent retries would both find no record, both do the work, and the
idempotency would be decoration. A repeat while the first is running is a 409
rather than a wait, because a request that blocks for the duration of a
deployment is worse than one that is told to retry.

The same key with a *different* body is a 409, not a replay. Replaying the first
response would silently discard the second request, which is a worse failure
than an error naming the client's own bug.

**Consequences.** A failed attempt releases its key, so a transient failure does
not lock the key for its 24-hour retention. Keys are pruned by the maintenance
sweep.

**Reversibility.** High.

---

## ADR-024 — The outbox is the producer contract; the relay is the only publisher

**Status:** Accepted (Phase 2, implementing ADR-003)

**Context.** ADR-003 chose a bus abstraction with a durable outbox. Phase 2 had
to decide who writes to the bus.

**Decision.** Producers call `events.publish(session, ...)`, which adds a row to
the **caller's** session and does not commit — the same discipline
`services/audit.py` and `services/metering.py` already use. A single relay moves
rows to the bus.

Publishing directly from a request has two silent failure modes: the state
commits and the publish fails, losing a fact; or the publish succeeds and the
transaction rolls back, announcing a fact that never happened. Writing the event
in the same transaction makes both impossible.

The relay stops a batch at the first failure rather than skipping past it.
Continuing would publish a later fact about an entity before an earlier one,
which is what partition ordering exists to prevent.

**Consequences.** Delivery is at-least-once, so consumers must be idempotent —
enforced centrally in `events/consumers.py` by recording `(consumer, event_id)`
before the handler runs, rather than left to each handler to remember. A
dead-lettered event is never pruned: it is a fact the platform failed to
announce, and deleting it would hide the failure.

**Reversibility.** High for the backend; the outbox contract is deliberately
fixed.


---

## ADR-025 — Drift is compared over the IR, never over the document

**Status:** Accepted (Phase 3)

**Context.** LLD §3.3 makes drift detection *"the governance differentiator"*.
The obvious implementation compares the fetched bytes with the stored bytes.

**Decision.** Compare the **canonical IR** (ADR-014's `openapi/fingerprint.py`)
and report differences in the API's own terms — "DELETE /pets/{id} was removed",
not "line 47 changed".

**Consequences.** A reformatted YAML, a reordered key map, a new comment, or a
migration from Swagger 2.0 to OpenAPI 3.x produces **no drift**, because the API
did not change. That is the whole value: a drift report that fires on
whitespace trains people to dismiss drift reports, and then the one that
matters is dismissed too.

Both hashes are kept — `content_hash` of the raw document and `ir_hash` of the
IR — because the difference between them is exactly "the file moved" versus
"the API moved", and sync needs to tell those apart.

`source_dialect` is excluded from the fingerprint for the same reason: it is
provenance, not meaning.

**Reversibility.** High.

---

## ADR-026 — Every change is classified, and the classification gates the apply

**Status:** Accepted (Phase 3)

**Context.** Build prompt §13 fixes five categories — BREAKING, NON_BREAKING,
SECURITY, DOCUMENTATION, METADATA — and the rule that *"breaking changes must
not automatically deploy unless policy permits it."*

**Decision.** `openapi/diff.py` classifies from the **caller's** side, following
`OpenAPITools/openapi-diff`'s taxonomy: removing an operation breaks callers;
adding one does not. Making a parameter required breaks callers who omitted it;
making it optional does not.

Two judgements are worth stating because they are not obvious:

- **Every security difference is SECURITY, whichever direction it moves.**
  Adding authentication is not "non-breaking because it is safer" — existing
  callers stop working. Removing it is not "non-breaking because callers keep
  working" — the API just became public. Both need a human.
- **A response disappearing from the document is DOCUMENTATION, not BREAKING.**
  The server may still return it; the document no longer mentioning it does not
  break a caller.

A source's `apply_policy` is `never` (default), `non_breaking`, or `always`. A
withheld change records **why** it was withheld, because "nothing happened"
must be explicable or the next person assumes the watcher is broken.

**Consequences.** `always` exists and is not the default: choosing it is a
decision somebody makes, and it is visible on the source.

**Reversibility.** Medium — the categories are a contract once reports exist.

---

## ADR-027 — Sync updates the definition; it never compiles or deploys

**Status:** Accepted (Phase 3)

**Context.** When an accepted change arrives, how far should it propagate?
Straight through to a redeployed runtime is the tempting answer.

**Decision.** Sync's job ends at *"the project's definition is current"*. It
does not recompile tools and does not redeploy runtimes.

**Consequences.** Compiling is where tool names, filters and auth are decided;
deploying is where a runtime is replaced. Each already has its own gate and its
own audit trail, and a sync that reached through both would bypass them from a
background loop nobody was watching. A test asserts an applied change leaves
`integration_db_id` untouched and the project `imported`.

The cost is that a fully automatic spec-to-runtime pipeline needs an explicit
step. That is the intended cost.

**Reversibility.** High — a later phase can add an opt-in chain, gated by
governance rather than by a background task.

---

## ADR-028 — Three connector verbs are implemented once, on the base class

**Status:** Accepted (Phase 3)

**Context.** Build prompt §12 requires seven verbs on every connector:
`connect`, `discover`, `fetch`, `validate`, `watch`, `detect_drift`,
`disconnect`.

**Decision.** Four are abstract and connector-specific. Three —
`validate`, `detect_drift`, `disconnect` — have exactly one correct
implementation and live on the base class.

A document's validity has nothing to do with where it was found, and drift is a
property of the API rather than of the transport that delivered it. Requiring
each connector to implement them would guarantee seven slightly different
answers to the same question.

**Consequences.** Every connector genuinely has all seven verbs; a test asserts
it. `disconnect` is a no-op for every connector today because all of them only
read — it exists so a connector that registers a webhook has somewhere to
remove it, rather than the interface changing after implementations exist.

**Reversibility.** High.

---

## ADR-029 — Postman collections are converted, and the conversion says what it inferred

**Status:** Accepted (Phase 3)

**Context.** LLD §3.3 lists Postman collections among the places definitions
live, and for many teams it is the *only* place.

**Decision.** Convert Collection v2.0/v2.1 to OpenAPI 3.0, and return
**conversion notes** with the result.

A collection records *examples of requests*, not a contract. There is no schema
for a parameter, only a value someone once sent — so types are inferred from
example values, and the notes say so. The generated tool describes what was
observed, not what is allowed, and a user who knows that can correct the parts
that matter.

Collection v1 is refused with the exporter setting that fixes it, rather than
half-parsed. An `Authorization` header in a collection becomes a *security
scheme*, never a parameter — emitting it as one would put a credential in the
tool's argument list.

**Consequences.** A converted collection is a starting point, and is honest
about being one.

**Reversibility.** High.

## ADR-030 — Documentation processing runs inline, on a job with per-stage checkpoints

**Status:** Accepted (Phase 4)

**Context.** LLD §3.5 puts documentation processing on a queue: upload returns a
`job_id`, a worker does the work, the client polls. That is the right shape at
scale — parsing a 300-page scanned PDF is minutes of CPU, not milliseconds.

There is no worker process in this install. Adding one would mean adding a
broker, a deployment unit, and a supervision story, none of which Phase 4 has.

**Decision.** Keep the LLD's *interface* — `POST /v1/documentation/jobs` returns
a job with a `job_id`, and a `DocumentJob` row records every stage — but run the
stages inline, in the request.

The alternative considered and rejected was writing a `queued` row and returning
immediately with nothing to run it. A job that says "queued" forever is a worse
lie than a slow request: the API would report a state the system cannot leave.

**Consequences.** Uploading a large document is a long request, and the caller
must be prepared to wait. In exchange, when the call returns, the answer is
true.

The checkpointing is not deferred with the worker. `completed_stages_json` is
written after each stage, so re-running a job resumes rather than restarts. That
is the LLD's requirement, it is useful now (a failed graph stage does not force
a re-parse), and it is what makes moving to a worker a change of *caller* rather
than a rewrite of the pipeline.

**Reversibility.** High. The worker calls `jobs.run_job` with a queued row.

---

## ADR-031 — A failed stage degrades the run; only parsing can fail it

**Status:** Accepted (Phase 4)

**Context.** LLD §3.5 states the failure rules directly: OCR that cannot read a
page flags the page and continues; a graph backend that is absent is skipped
with a warning. Neither is a failed job.

**Decision.** Three outcomes, not two. `processed` means every stage produced
what it should. `partial` means the document was processed and something was
lost, with `degradations` naming what and why. `failed` is reserved for parsing:
a document nobody can read has nothing downstream to do.

**Consequences.** `partial` is the *common* outcome in the default install,
because no embedding provider is configured, and that is the point — the state
says so rather than reporting a clean success over a document with no vectors.

Collapsing `partial` into `failed` would make people ignore real failures;
collapsing it into `processed` would hide a document whose middle forty pages
are missing. Both were considered and rejected for the same reason: an operator
must be able to tell the difference without reading logs.

**Reversibility.** High.

---

## ADR-032 — Extraction is deterministic and cited; the model-based extractor is declared, not written

**Status:** Accepted (Phase 4)

**Context.** LLD §3.5 wants business rules, workflows and terminology out of
prose — "what specs can't say". The obvious implementation is an LLM.

**Decision.** The default extractor is rule-based: deontic markers ("must",
"shall", "may not", "is prohibited") classified by weighted signals, procedures
recognised from numbered lists and arrow chains, definitions from a small set of
defining verbs. Every fact it produces carries a verbatim citation — the
sentence, its section path, its page, its character offsets — and the version of
the extractor that produced it.

A second extractor, `llm`, is declared and reports itself unavailable. It is not
written against a provider API this repository has not exercised (build prompt
§4). Asked to extract anyway, it returns nothing and records why.

**Consequences.** Recall is lower than a model's would be: a rule spread over
two sentences is missed, and it is English-only. Both limits are stated in the
module rather than discovered by a user.

Precision is what this buys, and it is the property that matters here. A wrong
business rule with a confident citation is worse than a missing one, because the
citation is what makes it believable. Determinism also means re-processing a
document produces the same facts, so a diff between two runs is a change in the
*document*.

**Reversibility.** High. `get_extractor` already selects between them, and the
pipeline records which one ran.

---

## ADR-033 — No embedding provider ships, and no substitute is used

**Status:** Accepted (Phase 4)

**Context.** Semantic retrieval needs vectors. There is no embedding model in
the default install. A hashing or TF-IDF vector is easy to produce and would
make the code path complete.

**Decision.** Ship no embedding provider, and no stand-in. Retrieval is BM25
over chunks, and every search response says `"mode": "lexical"` with the reason
semantic search is unavailable.

**Consequences.** A hash vector *is* a vector: it packs, it stores, it cosines,
and the search built on it returns ranked results. It is not a semantic
embedding, and a search built on it would look like semantic search while
behaving like lexical search with extra steps — the failure mode being that
nobody can tell which one they got.

Retrieval is written for both legs and fuses them with Reciprocal Rank Fusion,
so configuring a provider turns semantic search on without a rewrite.

**Reversibility.** High. `embeddings.set_provider` is the whole integration
point.

---

## ADR-034 — Original documents stay in the database by default

**Status:** Accepted (Phase 4)

**Context.** LLD §3.5 puts originals in object storage. Every extracted fact
cites the sentence it came from, so the original is evidence, not a cache: it
has to still be there when someone asks "says who?".

**Decision.** Two backends behind one interface. `database` (default) keeps
bytes on the `document` row — the same choice already taken for deployment
packages (ADR-015). `filesystem` writes content-addressed files under a
configured directory, for installs where documents are large enough that the
database is the wrong home.

No S3/MinIO backend is written. The seam exists so that when MinIO is actually
deployed it is a backend, not a rewrite.

**Consequences.** An upload ceiling (`DOCUMENT_MAX_BYTES`, 25 MB) is a real
limit rather than a formality. Deduplication is per tenant and by content
digest: the same bytes uploaded twice are one document and a second job.

**Reversibility.** High.

---

## ADR-035 — A flattened table is not a sentence

**Status:** Accepted (Phase 4)

**Context.** A PDF has no idea it contains a table. `pdftotext` flattens a
two-column page into lines with the gap preserved, an architecture diagram into
lines of arrows, and a table of contents into dot leaders. Every one of those
lines then looks, to a pattern that reads prose, exactly like a sentence.

This was found by running a real 31-page technical PDF through the pipeline. It
produced 76 "workflows" — mostly diagram arrows — and 54 "glossary terms", most
of which were table cells and contents entries.

**Decision.** `extraction/layout.py` decides whether a line came out of a
layout, and all three extractors consult it before matching. The signals are
deliberately structural rather than semantic: three or more spaces between two
words (a column gap), an arrow or a middot (a diagram), four or more dots (a
contents leader), a definition too short or too capitalised to be prose.

`chunking.split_sentences` was changed alongside it. It collapses *line breaks*
inside a sentence — hard wrapping is a rendering artifact, and leaving it in
made every prohibition in a wrapped document invisible — but deliberately does
*not* collapse runs of spaces, because that gap is the signal.

**Consequences.** On that document: terms 54 → 16, workflows 76 → 26, rules
8 → 4, and every survivor is a real statement from the text. Nothing extracted
from a prose document in the test suite changed.

Recall drops on documents that genuinely put a rule in a table, and that is the
trade being made. The reason it is the right one is the citation guarantee: a
rule citing two cells that happened to land on the same line is a citation
nobody can check, and one of those does more damage than ten missing rules,
because it makes the *checkable* citations less believable too.

**Reversibility.** High, and the checks are one small module with its own tests.

---

## ADR-036 — A runtime artifact is immutable, and its build hash is what enforces that

**Status:** Accepted (Phase 5)

**Context.** LLD §3.6 requires *"every stage produces immutable artifacts ·
identical inputs ⇒ identical outputs (deterministic builds)"*. Both halves are
easy to claim and easy to quietly break: a timestamp embedded in a generated
file, a dict iterated in insertion order, an `updated_at` column somebody adds
later.

**Decision.** `runtime_artifact` has no update path — no PATCH route, no
service function that writes an existing row — and `build_hash` is computed
from the generation inputs (project, IR version and hash, knowledge hash,
runtime, template version) together with the digests of the manifest and the
package bytes. Storage is keyed on `(org_id, build_hash)`, so generating again
from unchanged inputs finds the existing row and returns it instead of writing
a second one.

**Consequences.** Determinism is self-enforcing rather than asserted. If the
packager became non-deterministic, the second build would produce a different
`package_sha256`, a different `build_hash`, and a second row — which is exactly
what `test_generating_twice_from_unchanged_inputs_returns_the_same_artifact`
fails on.

The validation report is deliberately *outside* the hash. It records durations
and depends on which scanners the install has configured; folding it in would
mean a build that took longer was a different build.

A rebuild does not re-run validation. Re-validating bytes that already passed
would produce a second opinion about an artifact that cannot have changed.

**Reversibility.** Low for the immutability itself — the deployment gate rests
on it — and high for what goes into the hash, which is one function.

---

## ADR-037 — Validation has four states, and "blocked" is not "passed"

**Status:** Accepted (Phase 5)

**Context.** The LLD's validation list — *compile, dependency check, MCP
compliance, static analysis, security scan, unit/integration/smoke tests* —
mixes checks that need nothing but the package with checks that need a
vulnerability database and a scanner this repository does not ship and may not
invent (build prompt §4).

Two obvious options are both wrong. Marking an un-runnable check as passed
tells a reader the artifact was scanned when it was not. Failing every
artifact on an install with no scanner means the platform never deploys
anything.

**Decision.** Four states. `ok` ran and found nothing. `failed` ran and found
something, and the artifact is rejected. `blocked` could not run, is named in
the report and in the summary sentence, and does not count as a pass.
`skipped` does not apply.

An operator can supply their own scanner as a **command** —
`GENERATION_SECURITY_SCAN_COMMAND`, `GENERATION_VULNERABILITY_SCAN_COMMAND` —
run in a directory holding the unpacked package, where a non-zero exit fails
the gate. A command rather than an integration because the scanner an operator
trusts is theirs to choose, and because parsing a specific tool's JSON would
mean guessing at the output schema of a tool this code has never run.

**Consequences.** A default install produces artifacts that are `validated`
with `security_scan` and `vulnerability_scan` blocked, and the API says so on
every read. That is the honest description of what happened.

The checks that *are* implemented are real: `compile` compiles every generated
file, `mcp_compliance` checks names, descriptions and schemas against what
mainstream MCP clients accept, `dependencies` refuses a requirement from a URL
or without a constraint, `static_analysis` is an AST walk for constructs that
execute or deserialize (and is described as that, not as SAST), `secrets` is
pattern-based credential detection over the generated files, and `tests` runs
the package's own offline suite in a subprocess.

**Reversibility.** High.

---

## ADR-038 — Artifacts are signed with a local Ed25519 key, which is not Cosign

**Status:** Accepted (Phase 5)

**Context.** The LLD asks for *signed images*, and names Cosign in the
technology stack. This install builds no images and has no registry to push to,
so there is nothing for Cosign to sign; wiring a Sigstore integration against a
registry this code has never talked to would be inventing the part that matters.

**Decision.** Sign the **artifact's build hash** with an Ed25519 key the
operator holds (`GENERATION_SIGNING_KEY`, PEM or a base64 32-byte seed). With
no key configured, artifacts are stored unsigned and every read says so with a
`NOT_CONFIGURED` reason.

**Consequences.** What ships is a real cryptographic assertion — *this
platform, holding this key, produced an artifact with this digest* — verifiable
offline by anyone with the public key. It is **not** Sigstore: no transparency
log, no certificate chain, no keyless identity, and it does not sign a
container image. The traceability matrix says so rather than recording §3.6.7
as met.

A placeholder signature was rejected outright. A reader who saw a `signature`
field would reasonably believe it meant something.

When the key has been rotated since an artifact was signed, verification
reports the key as unavailable rather than as a failed check — a rotated key is
not tampering, and reporting it as such would train readers to ignore the field.

**Reversibility.** High. Signing is one module behind one function, and adding
a Cosign path later does not disturb it.

---

## ADR-039 — Documentation knowledge ships inside the generated package

**Status:** Accepted (Phase 5)

**Context.** LLD §3.6 defines the generator's input as *IR + documentation
knowledge + template*. Sutr had the IR and the template. The knowledge existed
in the control plane, behind a retrieval API.

**Decision.** Business rules, glossary terms and workflows extracted for a
project are written into the generated `tools.json` under `knowledge`, with
every citation intact, and each is attached to the tools whose significant
tokens it overlaps.

**Consequences.** A generated server is frequently run somewhere the platform
cannot reach, by an agent that will never call Sutr's retrieval API. A rule
that exists only in the control plane is a rule that agent will not follow;
shipping it with the tools is what makes it operative.

Two limits are deliberate. The attachment is a **match, not a claim** — nothing
is inferred and nothing is generated, and a rule that matches no tool is still
shipped at the top level rather than dropped. And the volume is capped (200
rules, 200 terms, 50 workflows) with the truncation counted in the manifest, so
a provider with a thousand extracted rules does not turn a 60 KB package into a
megabyte of prose without anyone noticing.

The same matching populates `operation_refs` on documented workflows, which is
what §3.5.6 was waiting for. It lives in `documentation/linking.py`, not in the
generator: `doc_workflow` is a documentation table and one service writes a
table.

**Reversibility.** High.

---

## ADR-040 — The Kubernetes provider delivers the package as a ConfigMap, and creates no Ingress

**Status:** Accepted (Phase 5)

**Context.** LLD §4.3.8 requires runtime isolation that only Kubernetes can
provide — namespace per provider, NetworkPolicy, ServiceAccount, resource
quotas, Pod Security Standards. But a Kubernetes Deployment needs an image, and
this platform has no build worker and no registry to push one to.

**Decision.** The generated package is mounted from a per-revision, immutable
ConfigMap over a base image the operator names (`KUBERNETES_RUNTIME_IMAGE`),
which must already have the package's dependencies installed. The ConfigMap
size limit is checked before the request, with a message naming the limit. No
Ingress is created; the returned URL is the in-cluster Service DNS name, and
the state record says in words that it is reachable in-cluster only.

**Consequences.** The isolation the LLD asks for is real and asserted in tests:
Pod Security Standards enforced at `restricted`, a default-deny NetworkPolicy
plus two narrow allowances (DNS, and egress to public addresses with every
private range excluded), a ServiceAccount with no token mounted, a resource
quota, a non-root container with a read-only root filesystem and all
capabilities dropped, and the credential in a Secret rather than in the pod
spec. Ingress is allowed only from namespaces the operator has labelled
`sutr.io/ingress: allowed`, which is what "cannot bypass the Gateway" means in
a NetworkPolicy.

Packages larger than about 900 KB cannot be deployed by this provider. That is
a real limit of the mechanism, and it is refused up front rather than at apply
time.

**NOT TESTED against a live cluster.** Every request is built against the
documented Kubernetes API and exercised against a stub transport that asserts
paths, methods and bodies. Nothing here has run against a real API server, and
the traceability matrix says NOT TESTED until it has.

**Reversibility.** High — it is one provider among six, disabled by default.

---

## ADR-041 — Drift is detected, not reconciled, and the difference is stated

**Status:** Accepted (Phase 5)

**Context.** LLD §5.7 asks for *"declarative + continuous reconciliation: drift
detection, version history, one-command rollback, full audit trail"*. Three of
those already existed: revisions are the version history, `POST /rollback` is
the one command, every transition is audited.

**Decision.** `deploy/drift.py` compares what the platform declared — which
revision should be live, which bytes it should be running, whether it should be
up — against what the provider reports, and `GET /api/deployments/{id}/drift`
returns the result. Nothing is corrected as a side effect of looking.

**Consequences.** This is **not GitOps**. There is no Git repository as the
source of truth and no controller reconciling towards one; the desired state is
the deployment row, and the comparison runs when someone asks. Argo CD and
continuous reconciliation stay NOT IMPLEMENTED in the matrix rather than being
blurred into this.

The comparison is deliberately narrow — only facts the platform actually
declared. A provider always reports fields Sutr never asked for, and reporting
those as drift would produce a detector that is never clean and therefore never
read.

"Could not check" is a distinct outcome from "nothing wrong": an unreachable
provider returns `checked: false` with the reason, never a clean report.

**Reversibility.** High.

---

## ADR-042 — One lifecycle vocabulary, fourteen states, taken from §3.1

**Status:** Accepted (Phase 6)

**Context.** The LLD describes the tool lifecycle twice. §3.1 (p. 11) lists
fourteen states and calls them *"the shared vocabulary for the whole platform"*.
§3.7 draws five (DRAFT → VALIDATED → DEPLOYED → UNDER_REVIEW → PUBLISHED) as the
registry's own picture. Implementing both would produce two enumerations that
have to be mapped onto each other and will drift the first time one gains a
state.

**Decision.** One machine, and it is §3.1's, because that is the one the
document says is shared. §3.7's five states are a summary of it. The six failure
states from §4.1 are branches off it, each with a recorded resume point:
`TRANSLATION_FAILED` resumes at `TRANSLATING`, not at `DRAFT`.

**Consequences.** `registry/lifecycle.py` is the only place the vocabulary
exists, `GET /v1/registry/lifecycle` reports the whole machine so clients need
not hard-code it, and a transition the table does not allow is refused with both
ends and the possible alternatives named.

Two deliberate asymmetries. A pipeline failure can be **abandoned back to
DRAFT** — a provider who decided the specification was wrong should not have to
invent a fix to escape. A `SUSPENDED` tool cannot: it has been published and has
subscribers, so it is restored, deprecated or archived, and reverting it to
DRAFT would orphan them.

The matrix's earlier claim of a "10-state lifecycle" was a miscount and is
corrected to fourteen.

**Reversibility.** High — one module and one table column.

---

## ADR-043 — The marketplace is a projection, and can lag

**Status:** Accepted (Phase 6)

**Context.** LLD §3.7: *"Registry = authoritative system of record. Marketplace
= read-optimized storefront derived from Registry events."* The simpler
implementation — a listing endpoint that queries the registry — would satisfy
every functional requirement and quietly make the two services one service with
two names.

**Decision.** `marketplace_listing` is a real projection, written **only** by
event handlers in `marketplace/projection.py`, consuming `tool.published`,
`tool.updated`, `version.created`, `pricing.updated`, `tool.deprecated`,
`tool.archived` and `subscription.created`. This is the platform's first
consumer of the event machinery built in Phase 2.

Each handler **rebuilds the listing from the registry record**, using the event
only as the signal that something changed. Patching from the payload would mean
every event carried a full copy of the tool, and the two would disagree the
first time a field was added to one and not the other.

**Consequences.** A listing appears when the relay delivers the event, not when
the provider publishes. That lag is real, so every listing carries
`projected_at` and `projected_from`, and the storefront response says so.

**Nothing authorizes off a listing.** Subscribing re-reads the registry record,
so a tool archived two events ago cannot be subscribed to because the storefront
has not caught up.

Two projection rules that are decisions rather than mechanics. **Deprecation
does not delist** — a tool nobody should start using must not vanish from under
the people already using it, so the listing stays and carries the note; archiving
delists. And **subscriber counts are counted, never incremented** — an increment
is a number that drifts the first time an event is replayed or a subscription is
cancelled behind the projection's back.

**Reversibility.** Medium. The projection could be replaced by a direct query,
but the honesty about staleness would go with it.

---

## ADR-044 — The trust score excludes what it cannot measure, and says so

**Status:** Accepted (Phase 6)

**Context.** LLD §3.7 specifies a 0–100 trust score over seven inputs — security
scans, validation success, runtime availability, error rate, governance status,
documentation quality, user ratings — and says it influences ranking. A single
number that ranks tools is exactly the kind of number that gets believed without
being understood.

Most of those inputs are unavailable for a new tool. Two obvious treatments are
both wrong: scoring an unmeasured input as zero punishes a tool for being new,
and scoring it as one flatters it.

**Decision.** An unavailable component drops out of **both** the numerator and
the denominator, its absence is named with a reason, and the result carries a
`coverage` figure — what fraction of the total weight was actually measured. The
weights sum to 100 so each reads as "up to this many points" and the arithmetic
is checkable by eye. Every component reports the evidence it used, not just a
value.

With no measurable component at all the score is **`null` with a reason, never
0** — the same rule the marketplace already applied to its null fields, applied
to the number that would be hardest to argue with.

**Consequences.** A freshly registered tool scores from governance standing and
documentation completeness alone, at 20% coverage, and the explanation says
which five inputs were not measured. A reader can tell 90-at-30%-coverage from
90-at-full-coverage, which is the difference between a promising tool and a
proven one.

A `blocked` security scan (Phase 5's fourth validation state) makes the security
component abstain rather than score zero: an install with no scanner has not
established that a tool is clean, and scoring it as if it had would launder a
gap into a number.

The documentation component measures **completeness, not prose**, and the
response says so — nothing here reads a description and forms an opinion of it.

**Reversibility.** High. Weights and components are data.

---

## ADR-045 — Governance is a gate here; the policy engine is not

**Status:** Accepted (Phase 6)

**Context.** LLD §3.7 requires lifecycle transitions to be *"gated by governance
policies"* and visibility and pricing changes to *"need governance approval"*. A
policy *engine* — rules that decide some of these automatically — is a different
thing and is Phase 9's work.

**Decision.** Build the gate now and leave the engine for its own phase. Four
changes produce a `RegistryChangeRequest` and take effect only when somebody
decides them: entering review, publishing, changing visibility, changing price.
Everything else applies immediately, because a gate on `TRANSLATING → IR_READY`
would be a human approving a parser.

A change request records **both sides** of the change. An approver deciding from
the request alone is deciding without knowing what it replaces, and the "before"
is what makes the audit trail readable a year later.

Price changes are **validated when requested**, not when decided: an approver
should not be the one who discovers the price was malformed, and a request that
cannot be applied should never reach them.

**Consequences.** Four eyes where four eyes are possible: a requester may not
decide their own change *when the organization has another eligible approver*.
Enforcing it unconditionally would make a solo install unable to publish
anything, which is not governance but a deadlock. When a self-decision does
happen, `self_decided` is true on the record, so the audit trail says what
occurred rather than implying a second person was involved.

Only one pending change per kind per tool. Two pending visibility changes would
race, and whichever was decided second would silently win.

**Reversibility.** High — Phase 9's policy engine slots in as an automatic
decider in front of the human one.

---

## ADR-046 — Pricing is history the marketplace shows, not money the platform moves

**Status:** Accepted (Phase 6)

**Context.** LLD §3.7 lists pricing as marketplace metadata and puts changes to
it behind governance. Sutr also has a real Stripe subscription for its *own*
platform plan. Conflating the two would mean a provider editing a marketplace
price could move real money.

**Decision.** `registry_pricing` rows are immutable and never edited: changing a
price supersedes the live row and writes a new one. A subscription snapshots the
price **by id**, so a provider raising their price does not silently reprice
everyone who already subscribed. Every serialized price and subscription carries
`billed_by_this_platform: false`.

Amounts are integer micro-units of the currency, because a price stored as a
float is a price that will eventually be off by a hundredth somewhere it matters.

**Consequences.** An invoice from March stays explainable in June, and "what did
they actually pay" is answerable from the row rather than reconstructed.

A tool nobody has priced serializes as `null`, **not** as a free plan. "Nobody
set a price" and "the provider chose to charge nothing" are different facts, and
rendering the first as the second would put a `free` badge on tools whose
provider never made that decision.

Nothing here charges anybody, and Phase 10's billing work is what would read it.

**Reversibility.** High for the shape; low for the immutability, which
subscriptions depend on.

---

## ADR-047 — The policy filter runs before ranking, and its exclusions are the answer when nothing matches

**Status:** Accepted (Phase 7)

**Context.** LLD §3.8 lists eight policy checks and says they run *"before
ranking"*. It would work either way — filter then rank, or rank then filter —
and the second is easier, because ranking does not then have to be re-run when a
filter changes.

**Decision.** Filter first, and keep what was excluded.

Two reasons for the order. Ranking is the expensive half of a discovery request,
and ranking a tool the caller may not use spends that half on an answer that was
never available. And a ranker that never sees an inaccessible candidate cannot
leak its existence through a score.

The second half — keeping the exclusions — comes from LLD §4.2's hot-path table,
which says a request with no match returns *"suggestions or 'tool not found'"*.
An agent handed an empty list learns nothing. An agent told *"three tools matched
your intent and you are not subscribed to them"* can subscribe. So every
exclusion carries the check that produced it and a reason, and when nothing
survives, the near misses are returned ranked by how well they matched.

**Consequences.** `POLICY_VERSION` is part of the cache key, so changing a rule
invalidates decisions made under the old one rather than serving them.

Two of the eight checks are **declared and unenforced**: there is no per-tool
provider policy in this build, and a registry tool is not yet linked to a
specific runtime, so runtime health cannot be checked per tool. Both are named
in `GET /v1/discovery/capabilities` with `enforced: false`, because a check that
silently never excludes anything is worse than a missing one — it looks like it
ran.

Archived tools are excluded for **everyone**, their own provider included. A
retired tool is not a recommendation, and the tenant that retired it does not
want it offered back.

**Reversibility.** High.

---

## ADR-048 — Fusion by rank, except when only one ranker contributed

**Status:** Accepted (Phase 7)

**Context.** Hybrid retrieval (LLD §3.8) means combining rankers whose scores
have no common unit — a BM25 score and a cosine similarity are not convertible.
Reciprocal Rank Fusion solves that by using only the *rank* each candidate
reached, which is why it is the standard choice.

Applied to a single ranker it is actively harmful, and this was found by running
it. RRF turns first and second place into 1/61 and 1/62 — two thousandths apart
— so a strong match and a weak one arrive at the ranking stage nearly tied, and
every later signal outvotes relevance. Live, `refund a customer payment` scored
`refunds-api` at 1.284 and `invoices-api`, which matched only the word
"payment", at 1.268.

**Decision.** Fuse by rank when two or more rankers contributed an ordering; use
the single ranker's own normalised scores when only one did. After the change
the same query scored 1.284 and 0.460.

This is the common path, not a corner case: no embedding provider ships
(ADR-033), so on a default install exactly one ranker contributes.

**Consequences.** The retrieval mode is reported by *what actually contributed*
— `keyword`, `vector`, `graph`, `hybrid` or `none` — rather than a two-valued
label that would have had to call a vector-only answer "lexical".

**Reversibility.** High; it is one function.

---

## ADR-049 — One BM25, folded for plurals

**Status:** Accepted (Phase 7)

**Context.** Documentation search ranks chunks; discovery ranks tools. Both want
Okapi BM25, and two copies of a scoring function are two copies that disagree the
first time one is tuned.

Separately, BM25 without any morphology treats `refund` and `refunds` as
unrelated terms. On a corpus of short, noun-heavy tool descriptions that is the
single biggest source of wrong rankings: a search for "refund a payment" ranked
an invoicing tool above a refunds tool, because the refunds tool says
"payments" and "refunds" and the query said "payment" and "refund".

**Decision.** `common/lexical.py` holds one BM25, one tokenizer and RRF, and
`documentation/retrieval.py` delegates to it. The tokenizer folds English
plurals — and nothing else.

Explicitly **not** a stemmer. Porter conflates words that mean different things
("universal" and "university" share a stem), and the failure being fixed here is
narrower than that. Short words are left alone, because `is`, `gas` and `bus` are
not plurals.

**Consequences.** Matched terms are reported in folded form (`refund`, not
`refunds`), which is slightly worse for display and much better for ranking.
Both services improved, and the documentation suite passed unchanged.

**Reversibility.** High.

---

## ADR-050 — The discovery cache is in-process, invalidated by generation

**Status:** Accepted (Phase 7)

**Context.** LLD §3.8 specifies the cache key — *"tenant + intent + policy
version"* — and its invalidation — *"on tool publish/update, policy change,
runtime unavailable"*.

**Decision.** The key is that, plus the ranking version and the caller's
requirements: both change the answer, and returning a `trust-first` ranking to a
caller who asked for `balanced` would be answering a different question.

Invalidation is by **generation counter**, not by deletion. Each tenant holds a
counter that registry events bump, and every key minted before the bump stops
matching. Deleting the affected entries instead would require knowing which
intents a tool could have matched, which is the search problem again.

A tool being published or archived bumps the **global** counter, because the
storefront is cross-tenant and one tenant's publication changes what every tenant
can discover.

**Consequences.** The cache is **per replica**. A shared one is Redis, and Redis
is not wired into this install; `GET /v1/discovery/capabilities` says so rather
than letting an operator assume a hit rate they are not getting. Correctness does
not depend on it — a miss recomputes, and an entry cannot outlive its generation.

A cached result is returned as a **copy** with `cached: true`. Marking the stored
object would mutate every earlier caller's result, so the second reader would
change what the first one is holding.

**Reversibility.** High.

---

## ADR-051 — The latency budget is cooperative, and says so

**Status:** Accepted (Phase 7)

**Context.** LLD §3.8 gives discovery a latency budget and a *"ranking timeout"*
degradation. The platform's target is under 500 ms (cover, §5.8) — a design
target that is NOT TESTED as a latency claim (ADR-017).

**Decision.** Elapsed time is checked **between stages**. When the budget is
spent before ranking, the request falls back to `relevance-only-1` — retrieval
order — which is a named ranking version rather than a special case, so the
fallback is a configuration the platform already understands.

**Consequences.** A stage that blocks for a minute will still take a minute. The
budget bounds how much *additional* work is started, not how long a started call
runs. That is a real limitation, and `capabilities` states it in words rather
than letting the word "budget" imply preemption. Enforcing it properly would
need every stage to be cancellable, and the two that could actually be slow — the
embedding provider and the database — are awaited calls this code does not own.

The constant is checkable even though the latency claim is not: a test sets the
budget to zero and asserts the fallback, which is a property of the code rather
than of the machine it runs on.

**Reversibility.** High.

---

## ADR-052 — Quality metrics exist; judgements do not ship

**Status:** Accepted (Phase 7)

**Context.** LLD §3.8: *"Quality tracked via precision / recall / NDCG."* All
three need relevance judgements — somebody saying which tools *should* have come
back for a given intent.

**Decision.** Implement the three metrics and ship **no judgements**.
`POST /v1/discovery/evaluate` takes intents and the tool ids the caller considers
relevant, runs real uncached discovery queries, and reports how it did.

**Consequences.** This is the difference between "quality is tracked" and "a
quality endpoint exists". Nothing reports a score until somebody says what a good
answer is, and `capabilities` says `judgements.shipped: 0` with the reason.
Inventing a judgement set would produce metrics measuring a fiction, and a
dashboard of them would be worse than no dashboard.

Recall is `null`, not 1.0, when a judgement lists nothing relevant: "found
everything" and "there was nothing to find" are different results. Evaluation
never reads the cache — measuring a cached answer would be measuring the cache.

**Reversibility.** High.

---

## ADR-053 — Four authorization layers, derived rather than guessed

**Status:** Accepted (Phase 8)

**Context.** LLD §4.3 shows *"AUTHORIZATION — four layers, evaluated in order"*
as a **figure**. In the condensed edition the four boxes are an image: the text
layer carries the caption, the ABAC example (*Finance role ∧ Organization=Bank-A
∧ Region=India ⇒ Allow Refund Tool*) and nothing else. The labels are not
extractable.

Two bad options. Guess four names and present them as the LLD's, which is
fabrication of exactly the kind §4 forbids. Or implement nothing until the
uncondensed document arrives, which leaves the platform with no ABAC at all
because one figure could not be read.

**Decision.** Implement four ordered layers derived from what the document
*does* say in text — it names tenant isolation, RBAC, ABAC and per-tool policy
as the platform's authorization machinery — and **say in the code and in the API
that the derivation is a derivation**:

    1. tenant   is the principal acting inside its own tenant?
    2. rbac     does the caller's role hold the permission?
    3. abac     do the attributes satisfy this tenant's rules?
    4. policy   the per-tool execution setting and approval requirement

`GET /v1/provisioning/capabilities` returns a `source` field saying the figure's
labels are not extractable and these four are derived. If the uncondensed LLD
names them differently, the mapping changes and the ordering does not.

**Consequences.** The order is not cosmetic: each layer is cheaper than the next
and refuses on less information, so a cross-tenant caller never causes a
tenant's rule set to be loaded. The layers that did not run are **absent** from
the decision rather than recorded as having agreed.

Layer 4 is **delegated**, not reimplemented. `approvals/policy.py` already
returns something richer than allow/deny — allow, deny, or require-approval —
and the enforcement point calls it immediately after layers 1–3. The split is in
`describe()` rather than hidden.

**Reversibility.** High.

---

## ADR-054 — An identity is not a credential

**Status:** Accepted (Phase 8)

**Context.** LLD §4.3.1 wants every user, agent, service and runtime to have a
unique identity. Sutr had users and API keys, and an API key is a credential
with a label — no attributes, no lifecycle of its own, and no way to express
"the finance agent" as distinct from "a key somebody named finance". Least
privilege is not expressible without that distinction.

**Decision.** `agent_identity` is separate from the credential that
authenticates it. A key may be bound to one; the identity outlives key rotation,
carries the tenant's attributes, and is what a pass is issued to. One key binds
to at most one identity — a credential that spoke for two would make every call
ambiguous.

Principals get a single comparable form: `sutr:user:…`, `sutr:agent:…`,
`sutr:service:…`, `sutr:runtime:…` and — the honest one — `sutr:api-key:…` for a
key with no identity bound. An unbound key is still an actor, and calling it an
agent would attribute its calls to something that does not exist.

**Consequences.** Revoking an identity **deactivates** it rather than deleting
it: the passes it was issued and the calls it made refer to it, and an identity
that vanishes turns its own history into unattributable rows. Revoking it also
revokes its live passes by default, because the situation the endpoint exists
for is a leak, and leaving outstanding passes valid would be the wrong default.

Attributes are the tenant's own claims about their own agent. Nothing verifies
them, and every response carrying them says `attributes_are_tenant_declared`.

**Reversibility.** High. The identity is optional throughout: a call with no
principal behaves exactly as it did before the layer existed, which is what made
it safe to add to a hot path hundreds of tests already cover.

---

## ADR-055 — A pass is the record of a decision, and the token is not stored

**Status:** Accepted (Phase 8)

**Context.** LLD §4.3: *"Short-lived · single-purpose · least-privilege · signed
· revocable — conceptually AWS STS temporary credentials. Issued by Provisioning
after the policy decision."* Five adjectives and an ordering, all easy to write
on a diagram.

**Decision.** Each adjective is enforced. Short-lived: a 30-second floor and a
one-hour ceiling, five minutes by default. Single-purpose: the pass names its
tools and its resource, and `aud` **is** the resource, so a pass minted for one
deployment is rejected by another — by the verifier, not by intention.
Least-privilege: requested tools are intersected with what the decision allowed,
so asking for more narrows the pass rather than widening it. Signed: a JWT in
the exact shape the generated runtimes have validated since Phase 5, so the
issuer had to match them rather than the reverse. Revocable: by `jti`.

Issuance takes a `Decision` and **raises on a refused one**. A pass without a
decision behind it would make the audit trail describe something that did not
happen.

**The token is never stored.** The row holds the claims, the decision that
produced them, and the identifiers needed to revoke it. A table of live bearer
tokens is a table whose compromise equals compromising every agent at once.

**Consequences.** The signing key is derived from `JWT_SECRET_KEY` by HKDF with a
distinct info string unless `ACCESS_PASS_SECRET` is set, so a pass and a session
token are cryptographically separated even though one secret is configured.
Reusing the JWT secret directly would make them interchangeable to anything that
only checked the signature.

**A generated runtime cannot see a revocation.** It validates offline — which is
what lets it keep serving when the control plane is down (LLD §2.2) — so
revocation is enforced where the platform is in the path, and the short lifetime
bounds the gap. Both the `capabilities` response and the revoke response say so.
A "revocable" credential whose verifier cannot check revocation is exactly the
half-truth §83 exists to prevent.

**Reversibility.** Medium. The claim shape is now a contract with every
generated server already in the field.

---

## ADR-056 — Deny wins, and no rule is no opinion

**Status:** Accepted (Phase 8)

**Context.** An ABAC layer added to a platform that already runs is added to
tenants who have written no rules. The two obvious defaults are both wrong:
default-deny makes every existing call fail the moment the layer ships, and
default-allow-with-precedence lets a stray allow rule overrule a deliberate
deny.

**Decision.** **Deny wins unconditionally** — priority orders which rule is
*reported*, never the outcome. **A rule set with no matching rule has no
opinion**, so a tenant with no rules keeps the behaviour they had before, and a
rule set about refunds says nothing about invoices.

Matchers are conjunctions; an absent matcher matches anything. Both sides are
normalised to lowercase on the way in, because a rule saying `"India"` and an
identity saying `"india"` would be a rule that silently never matches.

**Consequences.** Two authoring guards exist for the same reason: a rule with no
subject and no resource matcher is refused (it matches everything, which is a
policy nobody can reason about), and a misspelled resource attribute is refused
at write time rather than becoming a rule that never fires.

A third came from running it. A matcher with an **empty string** value —
`{"integration_id": ""}` — was accepted live and matched nothing. Empty values
are now dropped during normalisation, and validation runs on the normalised
form, so such a rule either becomes a readable broader rule or is refused.

**Reversibility.** High.

---

## ADR-057 — Vault stores secrets; it does not mint them here

**Status:** Accepted (Phase 8)

**Context.** LLD §4.3: *"Runtime → secret reference → Vault → temporary
credential → Provider API."* That sentence contains two features. Storing a
secret in Vault and reading it back is one. Minting a short-lived provider
credential from a Vault dynamic secrets engine is the other, and it needs an
engine, a role and a lease policy **per provider** — configuration this
repository has never seen.

**Decision.** Implement the storage half against Vault's documented KV v2 API,
and report the other half as NOT IMPLEMENTED with the reason. `secrets.describe()`
returns `dynamic_credentials.available: false`, so the presence of a Vault
backend does not imply credentials it does not mint.

This also required a real schema change: the `secret` row now carries a `ref`
column. With an external store the row holds a **pointer** and the credential is
genuinely not in the database — which is what §4.3.6 asks for, and which the db
and db_kms backends cannot provide because they store the ciphertext in the row.

**Consequences.** §4.3.6 remains a recorded deviation on the default install:
credentials are stored in the database and injected into runtimes as environment
variables. `secrets.describe()` states both plainly rather than leaving a reader
to infer them.

**NOT TESTED against a live Vault.** Every request is built against the
documented API and asserted against a stub transport — a design review, not a
deployment.

**Reversibility.** High; it is one backend among three.

---

## ADR-058 — Separation of duties is unconditional for policies, and conditional for the registry

**Status:** Accepted (Phase 9)

**Context.** Two gates in this platform now require a second person: the
registry's change requests (ADR-045) and policy approval (§5.2.10). ADR-045
enforces four eyes *only when a second eligible approver exists*, because a solo
install would otherwise be unable to publish a tool at all — a deadlock, not
governance. The LLD states the policy rule without any such qualification: *"no
admin may author and approve the same policy."*

**Decision.** The two differ, deliberately.

Policy approval refuses **unconditionally**. The reason the registry exemption
exists does not apply: a tenant with no governance policies loses nothing,
because an access policy with no active version contributes no rules and the
decision point keeps the behaviour it had. So the absolute rule costs a solo
install a feature it can live without, rather than the ability to ship.

The refusal names the fix — *"another administrator has to approve it. Add one,
or have an existing one review it."* — because an unconditional rule with an
unexplained error is a rule people work around.

**Consequences.** Both sides are recorded on every version:
`authored_by_user_id` and `approved_by_user_id`. Separation of duties is only
checkable if both are kept, and an audit that shows only who approved cannot
show that somebody else wrote it.

The same rule is applied to two other decisions for the same reason: a review
cannot be decided by whoever opened it, and an exception cannot be decided by
whoever requested it. An exception self-approved is a control removed.

**Reversibility.** High.

---

## ADR-059 — Compliance checks controls; it does not certify frameworks

**Status:** Accepted (Phase 9)

**Context.** LLD §5.2.6 names seven frameworks — ISO 27001, SOC 2, GDPR, HIPAA,
PCI DSS, RBI Digital Banking, DPDP Act — and says *"results become tool
governance metadata"*. The obvious implementation returns a status per
framework, and it would be a lie: most of what those frameworks require happens
outside software. Training, physical security, vendor management, incident
drills, breach notification — no code observes any of it.

**Decision.** A control catalogue in which every entry either **names a check
that reads a real fact**, or **says why it cannot be checked here**. Twelve
controls are automated; six are `manual` with the reason. A `manual` result is
never a pass, is counted separately, and does not block publication — only
failures do.

Every response carries the scope in words: *"Of the controls this platform can
observe, which hold. Controls marked manual are not assessed and are not passes;
this is not a certification against SOC 2."*

**Consequences.** The default install **fails** two controls by design —
plaintext secrets and no configured scanner — plus a third once governance
exists but has no active policy. Live, a SOC 2 run reported 9 pass, 2 fail, 4
manual out of 15. That is a more useful artifact than a green badge, and it is
the honest answer to a question the platform can actually answer.

An organisation's own controls (`org_controls`) is every automated check with no
external framework's scope attached, and it is what the approval workflow reads.

**Reversibility.** High — the catalogue is data.

---

## ADR-060 — The four fail-safes, and why the audit one is stronger than asked

**Status:** Accepted (Phase 9)

**Context.** LLD §5.2.11 gives four failure behaviours, and §5.2 gives the rule
they serve: *"Governance failures never silently permit unauthorized actions —
fail closed for high-risk operations."*

**Decision.** Each is implemented where the failure occurs, not as a generic
handler:

- **Compliance scan timeout** — `timed_out` is its own state, and the review's
  compliance stage blocks on it. Collapsing it into "failed" would lose the
  instruction.
- **Risk calculation failure** — the previous assessment is kept and flagged
  stale with the reason. A score is never overwritten with nothing: a score that
  vanishes makes every consumer treat the tool as unknown, which is a different
  claim from "we could not refresh this".
- **Policy deploy failure** — `rollback` moves the pointer back to the previously
  active version. Activation keeps `previous_active_version` precisely so this is
  one write rather than a reconciliation.
- **Audit write failure** — the LLD asks for *"queue durably; retry before
  completing sensitive ops"*. Sutr writes the audit row **in the same transaction
  as the action it describes**, so a failed audit write rolls the action back
  with it.

**Consequences.** The audit behaviour is **stronger** than the LLD's, and the
difference is worth naming rather than claiming compliance with a queue that
does not exist. A durable queue still has a window in which the act completed
and the record is in flight; a shared transaction has none. What the transaction
cannot do is survive the database being unavailable — but in that case the
action does not happen either, which is the correct failure.

`GET /v1/governance/capabilities` lists all four with the module that implements
each, so the claim is checkable rather than asserted.

**Reversibility.** Low for the audit transaction, which predates this phase and
much depends on it. High for the other three.

---

## ADR-061 — Seven governance points, six gated, and the seventh says so

**Status:** Accepted (Phase 9)

**Context.** LLD §5.2 claims *"continuous governance across both planes: upload
→ validation → generation → deployment → publication → invocation →
monitoring"*. Before this phase the traceability matrix recorded 1 of 7.

**Decision.** Rather than assert seven, `engine.POINTS` names each point, the
module that actually gates it, and whether it fails closed. Six do:

    upload        openapi/pipeline.py — parse, validate, lint before an IR exists
    validation    generation/validation.py — a failed check rejects the artifact
    generation    generation/pipeline.py — a rejected artifact is not deployable
    deployment    api/deployments.py — only a validated artifact deploys
    publication   registry/service.py + governance/review.py
    invocation    services/tool_pipeline.py — quota, four layers, tool policy

**Monitoring is not gated**, and says so: nothing evaluates governance
continuously against a running tool. Drift detection and metrics exist, but no
policy is evaluated on a schedule and no finding is raised from one.

**Consequences.** The claim in the capabilities response is `points_gated: 6` of
`points_total: 7` with the ungated one named — which is checkable, and a test
pins it. A governance layer that claims seven and covers one is worse than one
that covers six and says which.

**Reversibility.** High.

---

## ADR-062 — Risk runs the other way from trust, and nothing converts between them

**Status:** Accepted (Phase 9)

**Context.** LLD §5.2.7 specifies a risk score where *"lower = better, e.g.
18/100"*. Phase 6's registry trust score runs the opposite way — higher is
better — and the two are about the same tools.

**Decision.** Implement risk in the LLD's direction, mark every response
`lower_is_better: true`, and **never convert between the two**. A number that
means the opposite of what a reader assumes is worse than no number.

The rest follows Phase 6's pattern because the problem is the same: seven
components matching the LLD's seven named inputs, weights summing to 100, an
unmeasurable component **abstains** rather than defaulting, and `coverage`
reports what was measured. With nothing measurable the score is `None` — never
0, which here would mean "no risk at all".

**Consequences.** A caveat found by writing a test: **two scores are only
comparable at similar coverage**. A score is risk out of what was measured, so a
component becoming measurable changes the denominator, and a newly-measured
low-risk component can lower the number even though more is now known. The test
that discovered it asserts the component rather than the aggregate, and
`describe()['comparability']` states it.

Auto-approval is bounded by this: a `None` score never auto-approves, because
"nothing could be measured" is not "low risk" — and treating it as such would
auto-approve exactly the tools nobody has looked at.

**Reversibility.** High.

## ADR-063 — Metering records facts, pricing decides money, and the two never meet

**Status:** Accepted (Phase 10)

**Context.** LLD §5.1 splits monetization into metering and billing. The
tempting shortcut is to price at execution: the tool call already knows the
tenant, the tool and the plan, so writing an `amount_micros` onto the usage row
is one line and makes invoicing a `SUM`.

**Decision.** `record_usage` takes **no price parameter and writes none**, and
`sutr.services.metering` does not import `sutr.billing` at all. A metered event
is a fact about what happened; its price is evaluated at billing time from
whichever plan was published.

Three consequences follow, and all three are the reason:

- **Repricing is not retroactive.** A price written at execution time is the
  price forever, so a plan change either rewrites history or leaves the ledger
  inconsistent with the plan. Deciding at billing time means yesterday's usage
  bills at yesterday's published plan because the plan is versioned, not
  because the row was frozen.
- **The execution path cannot fail on billing.** A pricing bug cannot break a
  tool call it never touches.
- **Invoices are reproducible.** Regenerating a period from the same usage and
  the same published plans produces the same invoice, which is what makes a
  disputed invoice answerable.

The constraint is enforced structurally rather than by convention: one test
inspects `record_usage`'s signature for any of `amount`, `amount_micros`,
`price`, `price_micros` or `plan_id`, and another reads the module's source for
a `sutr.billing` import. Both fail the moment somebody takes the shortcut.

**Consequences.** Invoice generation must find a plan, and usage with no
published plan cannot be priced. That case is **reported** as `unpriced` on the
invoice rather than billed at zero or silently dropped — both of those are
decisions the platform is not entitled to make on a tenant's behalf.

**Reversibility.** Low. Prices written onto usage rows cannot be un-written.

## ADR-064 — Money is integers, rates are basis points, and tiering is graduated

**Status:** Accepted (Phase 10)

**Context.** LLD §5.1 lists eight pricing models — free, per-invocation,
per-API-call, per-second, subscription, tiered, hybrid and enterprise — with
discounts and tax.

**Decision.** All money is **integer micro-units** (1 000 000 = one unit of
currency) and all rates are **basis points** (250 = 2.5%). No float ever holds
an amount. Floats do not represent 0.1 exactly, and a rounding error in a
ledger is not a rounding error, it is a discrepancy somebody has to reconcile.

All eight models are implemented, dispatched from an `EVALUATORS` table keyed by
model name, and a test asserts the table's keys equal the declared model list —
so a ninth model cannot be advertised without an evaluator behind it.

Tiering is **graduated**: each band is charged at its own rate, and only the
units that fall inside it. The alternative — the whole quantity at the band the
total lands in — produces a cliff where one extra call raises the whole bill,
and the LLD does not ask for one.

Evaluation is a fixed pipeline — usage → plan → discount → tax → charge — and
each `Charge` carries its `steps` as readable strings, stored on the invoice
line. An invoice a tenant cannot check is an invoice a tenant has to trust.

Plan validation happens at **publish** time, not at evaluation time: a tiered
plan with no tiers is refused when somebody publishes it, where the mistake is
cheap, rather than at month end when it is a failed invoice run.

**Reversibility.** High for the models; low for the units, which are in the
schema.

## ADR-065 — The ledger only ever grows, and a correction is another entry

**Status:** Accepted (Phase 10)

**Context.** Mistakes happen: an invoice is issued against the wrong tenant, a
plan is misconfigured, a period is billed twice. Something has to undo them.

**Decision.** `sutr.billing.ledger` has an `append` and no update or delete
path — a test reads the module source and asserts neither exists. Undoing an
entry means `reverse()`, which writes a **compensating entry** in the opposite
direction, with the same amount, pointing at the original through
`reverses_entry_id`, and requiring a reason. Both rows remain: the mistake and
the correction are both visible, which is what an audit trail is for.

Amounts are always non-negative; `direction` (debit/credit) carries the sign, so
a negative amount is refused rather than quietly meaning a credit. Each entry
stores the `balance_micros` it produced, so a balance is readable at any point
in history without replaying the whole ledger.

Voiding an invoice therefore reverses every entry it produced rather than
deleting them, and the invoice row stays too, in state `void` with its reason.
No financial record is ever removed.

**Consequences.** The ledger grows monotonically and reconciliation reads more
rows than a mutable balance would. That is the intended trade.

**Reversibility.** Low, and deliberately so.

## ADR-066 — The revenue share is configuration, recorded per settlement run

**Status:** Accepted (Phase 10)

**Context.** The build brief §44 requires that the revenue-share percentage is
never hard-coded. LLD §5.1 describes provider settlement over marketplace usage.

**Decision.** The split lives in `settings.revenue_share_provider_bps`
(environment variable `REVENUE_SHARE_PROVIDER_BPS`, default 8000 = 80% to the
provider), read on **every** call rather than captured at import, so an operator
changing it does not need a restart. A run may override it for a one-off
arrangement, and the API exposes that override.

Whatever applied is then **written onto the settlement row**. A later change to
the configured default does not alter what an old settlement says it paid: the
row records the arrangement that was actually applied, not a pointer to
whatever the setting happens to be when somebody reads it.

Settlement covers **issued** invoices only. A draft is a calculation, not an
obligation, and settling one would create a provider payable against revenue
nobody has been billed for.

**Consequences.** A test asserts the module source contains no literal split, so
the constraint survives refactoring rather than resting on review.

**Reversibility.** High.

## ADR-067 — This platform computes money and does not move it

**Status:** Accepted (Phase 10)

**Context.** LLD §5.1 describes invoicing, settlement, payouts and dunning. A
platform that renders an invoice looks, to a reader of its API, like a platform
that collects one.

**Decision.** Implement the arithmetic and the records; do **not** implement
collection or payout, because that needs a payment processor integration whose
API is not in the LLD, and guessing one would violate the no-hallucination rule.
Say so in the payload rather than in a README:

- every invoice response carries `"collected_by_this_platform": false`
- every settlement response carries `"paid_out_by_this_platform": false`
- `dunning.describe()` reports `"collects_payment": false` and
  `"scheduler": null`
- `GET /v1/metering/capabilities` states all of it in one place

`record_payment` records a collection attempt made **elsewhere** and runs the
dunning step from it; `mark_paid_out` records a payout made elsewhere. Neither
calls anything.

Dunning retries on a configured interval up to a configured maximum
(`DUNNING_RETRY_HOURS`, `DUNNING_MAX_ATTEMPTS`) and then **stops** —
`next_attempt_at` becomes null and `exhausted()` is true — rather than retrying
forever. `due()` finds invoices whose next attempt has come round; no scheduler
calls it, and the field says so.

Status per the honesty rule: metering, pricing, invoicing, the ledger and
settlement are **IMPLEMENTED and tested**; payment collection and payout are
**NOT IMPLEMENTED — external processor integration required**.

**Reversibility.** High. A processor integration slots in behind
`record_payment` and `mark_paid_out` without changing the ledger.

## ADR-068 — Two pricing tables, because advertising a price is not billing one

**Status:** Accepted (Phase 10)

**Context.** Phase 6 added `registry_pricing`: what a marketplace listing tells
a prospective consumer a tool costs. Phase 10 adds `pricing_plan`: what an
invoice is computed from. Merging them is the obvious simplification.

**Decision.** Keep both, and document the distinction in the model itself. They
answer different questions, change on different schedules and have different
authorities: a listing's advertised price is provider-facing marketing that may
be a range or "contact us", while a plan is a versioned, published,
tenant-scoped object that arithmetic depends on. Merging them would mean either
that editing a listing changes what tenants are billed, or that a published
plan cannot be described in prose.

A plan is immutable once published: changing a price creates a **new version**
and supersedes the old one, so an invoice can name the exact plan version each
line was computed from — which it does, in `plan_versions`.

**Reversibility.** Medium.

## ADR-069 — A trace is only a trace if it is continued, so context propagates both ways

**Status:** Accepted (Phase 11)

**Context.** LLD §5.3 requires traces that span *Gateway → Discovery → Runtime
→ Provider*. Sutr already emitted a span around tool execution. That is not the
same thing: a span emitted here joins a caller's trace only if the caller's
trace context arrived with the request, and a provider's spans join ours only
if ours leave with the call.

**Decision.** W3C Trace Context in and out, at both boundaries.

- **In.** The gateway middleware extracts `traceparent`/`tracestate` from the
  inbound request and opens `sutr.gateway` as a *child* of the caller's span.
  A request that arrives without trace context starts its own trace.
- **Out.** Every provider call — HTTP and MCP alike — injects the current
  context into its request headers.
- **Sampling is `ParentBased`.** A request that arrived sampled stays sampled
  through this process regardless of the local ratio, because a cross-service
  trace with holes in it is worth less than no trace at all.

Two things are deliberately independent. The **correlation id always
propagates**, with or without the OpenTelemetry SDK, because it is ours and
costs nothing. The **trace context propagates only when a span is recording**,
because advertising a `traceparent` for a trace nobody is keeping invites a
provider to parent its spans onto an id that leads nowhere.

**Consequences.** Providers receive two extra headers. A provider that does not
speak trace context ignores them; one that does gets a call it can correlate,
which is the point.

**Reversibility.** High. Both directions are one module, `observability/propagation.py`.

## ADR-070 — Spans carry the shape of a call and never its content

**Status:** Accepted (Phase 11)

**Context.** A span is the most tempting place in a codebase to put a debugging
aid: the arguments, the URL, the search query. It is also the one place that
leaves the process by design and lands in a backend that every operator can
read and no tenant controls.

**Decision.** Span attributes describe *shape*. Specifically, and asserted by
test:

- Tool arguments and results never appear — only the integration, the tool
  name, the outcome and the duration.
- The provider span carries `server.address` (the host) and the method, **never
  the URL**: credentials can ride in a query string (ADR-009).
- The discovery span carries the *length* of the intent, not the intent — a
  search query is a user's sentence and can contain anything.
- Every span carries the correlation id, because that is the join between a
  trace and the log lines written underneath it.

The same reasoning already governs the log formatter, where redaction happens
at the boundary rather than at call sites. Spans get the stricter rule — an
allow-list of attributes rather than a redaction pass — because a span
attribute is set deliberately, one at a time, and an allow-list is enforceable
by reading the call site.

**Consequences.** A trace will not tell you *what* an agent asked for. The
tenant-scoped call log will, under a permission check, to the tenant that owns
it.

**Reversibility.** High, and it should stay reversible in only one direction.

## ADR-071 — Tenant telemetry is served from our own tables, not from the shared backends

**Status:** Accepted (Phase 11)

**Context.** §5.3 asks for *tenant-isolated telemetry*. The stack it asks for —
one Prometheus, one Loki, one Tempo — is shared: every series, line and span
from every organisation lands in the same place. Two ways to give a tenant a
view of it are available and both are wrong here. Proxying queries with a
per-tenant filter makes correctness depend on getting a query rewriter right,
forever. Multi-tenant headers (`X-Scope-OrgID`) are enforced by whoever sets the
header, which is not isolation.

**Decision.** `/v1/observability` serves a tenant its own telemetry from *this
platform's own tables*, where every row already has an owner: call rate, error
ratio, latency percentiles, and the timeline of one operation, scoped to the
caller's organisation by the query itself. A tenant never reaches Prometheus,
Loki or Tempo, and the capabilities report says so in a field
(`trace_backend_queries: false`) rather than leaving an empty response to be
interpreted.

Complementing that, `/metrics` keeps its existing rule — **no tenant labels
anywhere** — now enforced by a test that walks every exported series and fails
on a label named for an org, tenant, user or key.

An operation carries the `trace_id` of the trace that recorded it, when there
was one. That is the seam: the tenant sees its own record, and an operator
holding both can open the trace.

**Consequences.** A tenant's view is bounded by what this platform stores — its
own calls — rather than by what the backends hold. Percentiles are computed
over at most `MAX_SAMPLE` rows and the response says when it was truncated.

**Reversibility.** High. A future query proxy would be an additional source,
not a replacement.

## ADR-072 — Queue gauges are sampled on the scrape, and a failed sample reads as stale

**Status:** Accepted (Phase 11)

**Context.** §5.3.5 asks for queue depth and lag. A counter can be incremented
where the thing happens; "how many events are waiting" has no such moment —
nobody is present when a queue *stays* long. It has to be sampled.

**Decision.** Sample on the scrape. `/metrics` runs two counts and one `min()`
against the outbox before rendering, so the number a scrape returns was true
when it was returned — which is the property an alert on queue depth depends
on.

The failure path is the other half of the decision: a sample that cannot read
the database **counts itself and leaves the gauges alone**. They then read as
stale, and the next successful sample corrects them. Zero would be a lie an
alert would act on, and raising would take `/metrics` down with the database —
removing the counters that would have explained the outage. A dedicated
counter, `sutr_telemetry_sample_failures_total`, has its own alert saying that
the two queue alerts are currently unreliable.

**Consequences.** A scrape does a little database work. The queries read two
indexed columns and no payloads, which a test pins by reading the source.

**Naming.** The LLD's list says "Kafka lag" and "billing lag". This install's
metric is `sutr_event_relay_lag_seconds`, because on an install with no broker
the outbox *is* the queue, and naming a metric after a component that may not
exist is how a dashboard comes to describe something else.

**Reversibility.** High.

## ADR-073 — The observability stack ships as validated configuration, and says it is not HA

**Status:** Accepted (Phase 11)

**Context.** §5.3 names eight components and requires that all of them run HA.
A directory of plausible YAML would satisfy a checklist and nothing else.

**Decision.** `deploy/observability/` ships seven of the eight as a pinned,
single-replica compose stack, and states the gap in a table in its own README
rather than in a commit message. kube-state-metrics is absent because it reports
on a Kubernetes cluster and this is a compose file; HA is Phase 12.

What makes the assets more than files is what is checked:

- The configurations were validated against the real binaries at the pinned
  versions (`promtool check config`, `amtool check-config`, `otelcol validate`,
  `loki -verify-config`, `tempo -config.verify`) and the stack was started and
  queried end to end.
- The suite asserts, on every commit, that **every metric named in an alert or
  a dashboard panel is a metric this build exports**, that every alert points
  at a runbook section that exists, and that every image is pinned to an exact
  version. Renaming a metric fails the build.

That split is deliberate: the binary-level validation needs Docker and a
network, which CI for this repository does not have, so the README says it was
done rather than claiming it is guaranteed. What CI *can* check is the drift
that actually happens — an application change that leaves the dashboards
pointing at series nobody emits.

**Consequences.** An alert cannot be added for a metric that does not exist
yet. That is the intended order of work.

**Reversibility.** High.

## ADR-074 — Work that must run once runs under a lease, not a lock

**Status:** Accepted (Phase 12)

**Context.** Running the API on several replicas is the point of scaling out.
Running the *background loops* on several replicas is a bug, and a quiet one:
`sweep_deployments` meters runtime minutes, so two replicas sweeping the same
five minutes bill a tenant for ten. Nothing raises, nothing logs, and the
number is simply wrong. Before this phase the only defence was a comment
telling operators to set `EVENT_RELAY_ENABLED=false` on all but one replica —
a rule that is followed until somebody scales the deployment and does not read
it.

**Decision.** A lease table, `leader_lease`, one row per job. A replica takes
the row, renews it every 15 seconds while it works, and stops renewing when it
dies; another replica takes over once the 45-second lease expires. Four jobs
are leased — maintenance, the deployment sweep, source sync and the event relay
— and the lease is *per job*, so a standalone relay worker and an API replica
can hold different ones.

A lease rather than a PostgreSQL advisory lock, for three reasons:

- **It works through a connection pooler.** A session-level advisory lock in
  PgBouncer's transaction mode is worse than useless: the lock lands on
  whichever server connection served that statement, and can be held by a
  pooled connection long after the client that took it has gone.
- **It works on SQLite**, so the behaviour is tested in the ordinary suite
  rather than only in an environment with Postgres.
- **It is visible.** `SELECT * FROM leader_lease` answers "which replica is
  sweeping?", and so does `GET /v1/platform/leadership`.

The trade is the usual one: a leader that stalls longer than its lease may
still believe it holds it while another takes over. Every leased job is
therefore idempotent or sampling — an overlap over-counts one sample rather
than corrupting a total — and that constraint is why the deployment sweep can
be leased at all.

**Consequences.** Takeover after an unclean death takes up to a minute (lease
plus poll), which was measured at 61 seconds. A clean shutdown releases the
lease and the takeover is immediate.

**Reversibility.** High. The lease is one module and one table.

## ADR-075 — A standby refuses writes with a reason, rather than letting the driver do it

**Status:** Accepted (Phase 12)

**Context.** The LLD's multi-region model is *"sync within region, async across
regions"*: one region owns the writes and the others follow. With PostgreSQL
streaming replication a follower's database physically refuses writes, and the
application in that region turns every write into a 500 carrying *"cannot
execute INSERT in a read-only transaction"* — an error an agent cannot act on
and will retry against the same instance.

**Decision.** `PLATFORM_MODE=standby` makes an instance know what it is. It
serves reads, runs no leased background jobs (all of them write), and refuses
writes with a 503 that names the mode, names the region that owns writes, and
carries `Retry-After`.

Three details are deliberate:

- **Anything unrecognised means active.** A typo in an environment variable
  must not silently turn a region that serves writes into one that refuses
  them.
- **Some POSTs read.** Discovery search and the policy decision point take
  structured input too large for a URL and write nothing, so they are
  allow-listed by exact path — with a test asserting each path is a real route
  and a second asserting those handlers contain no `session.commit()`.
- **A standby is *ready*.** Serving reads is what it is for; its refusal of
  writes is a per-request 503, not a reason to take the replica out of
  rotation.

**What this is not.** A failover system. Nothing here promotes a standby,
routes traffic between regions, or measures replication lag. Promotion is a
database operation and traffic is a DNS one; inventing an application-level
version of either would be inventing a system that does not exist.

**Reversibility.** High.

## ADR-076 — Liveness and readiness answer different questions

**Status:** Accepted (Phase 12)

**Context.** `GET /health` returned `{"status": "ok"}` unconditionally, which is
the right answer to one question and the wrong answer to another. An
orchestrator asks two: *is this process wedged?* and *should this replica get
traffic?*

**Decision.** Three endpoints. `/health` is unchanged — the compose
healthcheck, `fly.toml` and existing deployments point at it — and is an alias
for liveness. `/health/live` checks the process and deliberately nothing else.
`/health/ready` checks the database and returns 503 when it cannot be reached,
along with the instance id, the region, the mode and the leases this replica
holds.

The split is load-bearing: a liveness probe that failed during a database
outage would restart every replica at once and turn a recoverable incident into
a longer one. A readiness probe that ignored the database would keep sending
traffic to a replica that cannot serve it.

The readiness body carries no connection detail — hosts, users and ports go to
the log, because the probe is unauthenticated.

**Reversibility.** High.

## ADR-077 — Infrastructure is shipped only for stores this build actually uses

**Status:** Accepted (Phase 12)

**Context.** LLD §5.4 names eight data stores. The phase plan calls for "HA
compose/Helm for Postgres, Kafka, Redis, OpenSearch, Qdrant, Neo4j, MinIO,
Vault". Six of those have no client anywhere in this codebase: discovery's
lexical retrieval is SQL, embeddings live in a table, the knowledge graph is
two tables, documents are in the database or on a filesystem, caches are in
process.

**Decision.** Ship HA configuration for PostgreSQL, which this build uses, and
for nothing it does not. `deploy/ha/README.md` carries a table naming each of
the LLD's stores and whether the application has a client for it, so the gap is
readable rather than implied.

Shipping a Neo4j StatefulSet that nothing connects to would satisfy a checklist
and mislead every reader of the repository: it would look like the knowledge
graph runs on Neo4j. When one of those stores acquires a client, its HA
configuration arrives with it.

The same principle sets the honesty bar for what *is* shipped: the compose
stack was started and exercised — replication, pooling, load balancing,
leadership failover, standby refusal, a backup/restore round trip — and the
Helm chart was rendered and schema-validated but **never applied to a live
cluster**, which the chart's own `NOTES.txt` says out loud.

**Consequences.** This phase does not close §5.4's rows for the six absent
stores, and the traceability matrix still reads NOT IMPLEMENTED for them. That
is the accurate status.

**Reversibility.** N/A — it is a rule about what not to write.

## ADR-078 — A failing provider stops being called, and only real failures count

**Status:** Accepted (Phase 13)

**Context.** LLD §5.8 names the circuit breaker first among its five
failure-handling patterns. The reason is specific: when an upstream is down,
every call still costs a worker, a connection and the caller's patience, and
the answer is known in advance. Measured on a live instance, a provider whose
host refused connections cost **10.1 seconds per call**; with the breaker open,
the same call returned in **9 milliseconds**.

**Decision.** Three states — closed, open, half_open — keyed by *host* rather
than by integration, because two integrations pointing at the same upstream
share its fate.

Two details carry most of the value:

- **half_open lets exactly one call through.** Not a percentage, not a rate:
  one. A provider that has just come back must not be load-tested by the thing
  protecting it.
- **Only failures that say something about the provider count.** A refused
  connection does. A 404 does not — that is a provider working correctly and
  disagreeing with the request, and opening a circuit on a run of them would
  take a healthy provider away from every caller. A 5xx does count: that is the
  provider saying it is broken. An unsafe-URL refusal does not: the call never
  left this process, and blaming a provider for it would quarantine one that
  was never asked anything.

**A refused call raises rather than returning an error result.** It never
happened, so recording it as an execution would meter it, log it as `executed`,
and count it against a provider that was not called.

**State is per process**, and that is a trade rather than an oversight: a
shared breaker would need a store on the hot path and would let one replica's
bad network stop every other replica's traffic to a provider that is fine. The
cost — N replicas make up to N trial calls per cool-off — is declared in
`platform/scaling.py` with everything else that is per process.

**Reversibility.** High.

## ADR-079 — Retry only what did not arrive, and only where repeating is safe

**Status:** Accepted (Phase 13)

**Context.** Retrying is the easy half of a retry policy. The half that matters
is deciding what *not* to retry, because retrying the wrong thing turns a
struggling provider into an unreachable one and executes non-idempotent
requests twice.

**Decision.** Two rules, both narrower than a typical retry helper:

- **Only transport failures.** A connection refused, reset or timed out did not
  reach the provider's application, so repeating it cannot repeat a side
  effect. An HTTP response — of any status — did reach it, and this platform
  does not know whether the tool it called was idempotent. A 500 from a
  provider is therefore reported, not retried.
- **Only safe methods.** Even a transport failure can leave a request
  half-applied: a POST whose response was lost may have been executed. GET,
  HEAD and OPTIONS are safe by definition; nothing else is retried.

Backoff is exponential with **full jitter**. Without jitter, every caller that
failed at the same moment retries at the same moment, and the provider that was
recovering receives the whole herd.

Timeouts are separated into connect, read and pool rather than one number,
because waiting for a handshake to a host that is gone is a different failure
from reading a slow report — and the fast one failing fast is what lets the
breaker notice a dead host in seconds.

**Consequence, measured.** A failing GET costs two connect timeouts rather than
one, because it is retried. That is the intended trade and it is visible in the
chaos run.

**Reversibility.** High.

## ADR-080 — A gate that has never run is not a gate

**Status:** Accepted (Phase 13)

**Context.** Six security workflows were written in Phase 1 and never executed;
the traceability matrix recorded them as NOT TESTED, which was accurate and
comfortable. Running them was the whole of Phase 13's security work.

**Decision.** Every gate is executed against this repository before it is
claimed. What that produced is not a formality:

- The dependency audit **would have failed on every run** for a reason
  unrelated to security — `--strict` against the installed environment trips
  over this repository's own unpublished package. It now audits the locked set.
- It then found **30+ advisories**, including a Starlette
  authorization-bypass class that this platform's middleware is exactly the
  kind of code to be caught by.
- Secret detection **would have failed on day one** with twelve false
  positives, which is how gates get disabled.
- SAST found **84 findings**, of which 62 were fixed rather than suppressed —
  including a root-running production image that contradicted the Helm chart's
  own `runAsNonRoot` declaration, and an action reference (`trivy-action@0.28.0`)
  that does not exist.

Findings are **fixed by preference and accepted by exception**. Every
acceptance is recorded twice: at the site, in a `nosemgrep` or allowlist entry,
and in `docs/SECURITY_GATES.md` with the reasoning. Neither can drift without
the other becoming obviously wrong.

**What is still not claimed.** The workflows have never run *in GitHub
Actions*. They are NOT TESTED in CI, and `docs/SECURITY_GATES.md` says so in
those words.

**Reversibility.** N/A — it is a rule about evidence.

## ADR-081 — Load and chaos results name the machine, or they are not results

**Status:** Accepted (Phase 13)

**Context.** The LLD's cover carries four numbers — 100 000
invocations/minute, 10 000 concurrent agents, 1 000 000 searches/day, 99.95%
availability — and the build prompt forbids claiming them until they have been
measured against the corresponding requirement.

**Decision.** `qa/load/` and `qa/chaos/` hold scripts that are actually run,
and `RESULTS.md` records each run with the machine it ran on. The first
recorded baseline is **484 req/s across four read endpoints, 0 failures, p95
124 ms** — on one uvicorn worker, against SQLite, with the load generator
sharing the host.

That number does not approach the LLD's targets and is not offered as
evidence about them; §5.8's performance rows stay **NOT TESTED**. What it is
for is narrower and real: a repeatable figure per endpoint, so a change that
makes the gateway several times slower is noticed by somebody other than a
customer.

Chaos is treated the same way. Seven scenarios are named; **three have been
run** and one partially. The other three need a Kubernetes cluster, a
fault-injection proxy and a second region, and their status is "not run" rather
than a carefully written script implying otherwise — a scenario that has never
been executed proves nothing about the system.

**Reversibility.** N/A.
