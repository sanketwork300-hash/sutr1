# Sutr

## What This Is

A universal tool gateway for AI agents. One place to manage every external capability an agent can call — remote MCP servers or plain REST APIs.

## Stack

| Concern | Choice |
|---------|--------|
| Server framework | FastAPI + uvicorn |
| CLI | TypeScript + Commander.js |
| ORM | SQLModel (SQLAlchemy 2.0 + Pydantic) |
| Migrations | Alembic |
| Database | SQLite (self-hosted) / PostgreSQL (SaaS) — swap via `DATABASE_URL` only |
| HTTP client (server) | httpx (async) |
| MCP protocol | `mcp` Python SDK |
| Server package manager | `uv` (always `uv run`, `uv add`, `uv sync` — never pip) |
| CLI package manager | `pnpm` (always pnpm — never npm or yarn) |
| Linting/formatting | ruff (server), tsc strict (cli) |
| Format on completion | **Always run `uv run ruff format` from `server/` before committing** |
| Testing (server) | pytest + pytest-asyncio + httpx |
| Testing (cli) | vitest |

## Architecture

```
Claude Desktop / VS Code / any MCP client
           │  MCP protocol (StreamableHTTP)
     ┌─────▼──────────────────────┐
     │     Sutr server        │  :4747  (Python / FastAPI)
     │  /mcp  → MCP endpoint       │  ← aggregates all integrations
     │  /api  → REST API           │  ← management
     │  /docs → Swagger UI         │  ← auto-generated
     └──────┬──────────────────────┘
            │ HTTP
     ┌──────▼──────┐      ┌──────────────┐
     │  CLI         │      │  UI (future) │
     │ (TypeScript  │      │              │
     │  Commander)  │      └──────────────┘
     └─────────────┘
```

## Key Architectural Decisions

- **CLI and server are fully independent.** Different packages (`server/`, `cli/`), different processes, communicate over HTTP only. The CLI never starts or manages the server process.
- **SQLite / PostgreSQL is purely a config swap.** Change `DATABASE_URL`, nothing else. No code differences between self-hosted (SQLite) and SaaS (PostgreSQL).
- **Integration definitions are Python classes.** Each bundled integration subclasses `RemoteMcpIntegration` or `CustomIntegration` with all fields as defaults. Instantiated with no args.

## Code Style & Formatting

After completing any work on the server, always run:

```bash
cd server && uv run ruff format
```

Do this before committing. No exceptions.

## Modularity (essential)

This is a hard rule, not a preference:

- `server/src/sutr/api/` — **one file per API resource.** Never combine resources into one file. If a resource grows beyond a single file, it becomes a directory with sub-files by concern.
- `cli/src/commands/` — **one file per command group.** Same rule.
- Prefer 5 focused 50-line files over 1 sprawling 250-line file.
- Never create a `utils.py` or `helpers.ts` dumping ground. If logic is shared, name it after what it actually does.

## Integration Types

| Type | Description | Example |
|------|-------------|---------|
| `remote_mcp` | MCP server hosted by vendor, connect via HTTP | PostHog, GitHub |
| `custom` | Plain REST API, proxy makes HTTP calls and presents as MCP tools | Stripe (via OpenAPI) |

A `custom` integration is always produced by the MCP builder from an OpenAPI specification —
the hand-rolled builder was removed, because deriving paths, parameters, and auth from a
document beats asking someone to retype them.

## Connected Accounts

OAuth grants sutr holds on a user's behalf, in `server/src/sutr/connections/`. Two uses, one
mechanism: **GitHub** authorizes reading specifications out of repositories, and **Google Cloud
/ Azure / AWS** authorize running generated MCP servers in the user's own account.

Rules that must hold:

- A connection is scoped to `(org, user, provider)`. It is a *personal* grant: another member
  must never be able to borrow it, and an API key cannot open one (it has no identity to revoke).
- Tokens live in `secret` rows through the configured secrets backend. The `provider_connection`
  row holds pointers, an expiry, and a display label — never a token.
- Everything that needs a token calls `connections.store.access_token()`, which refreshes when
  due. No caller decides whether a refresh is needed.
- The callback's `redirect_after` must be a relative path. An open redirect there turns an OAuth
  flow into a phishing hop.
- **AWS has no OAuth for its own service APIs.** The OAuth path for AWS is IAM Identity Center's
  `sso-oidc` device grant (RFC 8628); only that token is stored, and each operation exchanges it
  for short-lived role credentials.

## One request definition, two runtimes

`server/src/sutr/runtime/request_builder.py` is the single definition of how a compiled tool becomes
an HTTP request — URL building, query and header wire names, body assembly per encoding, credential
placement. It is **dependency-free on purpose**: `openapi/packaging.py` reads its source and embeds
it verbatim into every generated standalone package.

The generated runtime is therefore not a copy that resembles the gateway's — it *is* the gateway's.
A parametrised test runs both implementations over every body encoding and asserts the two produce
identical requests. If you change request construction, change it there and nowhere else.

## Deployment Providers

`server/src/sutr/deploy/` — one narrow interface, five implementations:

| id | Target | Pipeline |
|----|--------|----------|
| `docker` | The sutr host's own daemon | build image → run container on 127.0.0.1 |
| `gcp` | Cloud Run | GCS upload → Cloud Build → Artifact Registry → Cloud Run |
| `azure` | Container Apps | ACR source upload → ACR Task → Container Apps |
| `aws` | App Runner | S3 → CodeBuild → ECR → App Runner |
| `swaraj` | Swaraj Cloud | **BLOCKED** — interface only; every operation refuses with `DOCUMENTATION_REQUIRED`. Its API is not documented anywhere this code can reach, and inventing it is forbidden (ADR-006). |

Deployments are **versioned**. An update is a new revision of the same deployment, never a delete
followed by a create: each revision builds a distinct, retained artifact (the image tag carries the
revision number), history lives in `deployment_revision`, and a rollback re-runs a stored package
rather than rebuilding from source that may since have changed.

A provider declares its own `config_fields`, so the builder renders its form without knowing the
provider; adding a provider is a server change only. Credentials are never cached on the
deployment row — `deploy/credentials.py` resolves them fresh for every operation, because cloud
credentials are short-lived by design.

## Service boundaries (read before adding a module)

`server/src/sutr/platform/boundaries.py` declares which service owns which modules and which tables,
and which of the three planes it belongs to. `tests/test_platform/` enforces three rules:

1. **Every module belongs to exactly one service.** Add a module without claiming it and the build
   fails. This is deliberate: an unowned module is one nobody maintains.
2. **Every table is written by exactly one service.** Other services read through the owner, never
   by joining to its tables. This is database-per-service in the form one database can hold.
3. **Data-plane modules do not import control-plane modules.** This is the testable form of "when
   the control plane is down, existing tools keep working" (LLD §2.2).

Rule 3 does not hold yet. The violations that remain are listed in `KNOWN_PLANE_VIOLATIONS` with the
phase that removes each one, and the test fails both on a *new* violation and on a *stale* entry —
so the list can only shrink. **Do not add to it to make a test pass**; fix the import, or if it is
genuinely unavoidable, add an entry that names the phase resolving it.

## Events

`server/src/sutr/events/` — the backbone. Three rules:

- **Producers call `events.publish(session, ...)` and never publish directly.** It adds a row to the
  *caller's* session and does not commit, so a fact and the state change it describes commit
  together or not at all. Publishing inside a request has two silent failure modes: a lost fact, or
  an announced fact that never happened.
- **Only the relay publishes.** That is what makes "every event goes through the outbox"
  enforceable rather than aspirational.
- **Consumers do not need to be idempotent by their own construction.** `events/consumers.py`
  records `(consumer, event_id)` before calling a handler, so a handler is not called twice for one
  event. It must be correct when called *once*, which is a far easier property to hold.

Kafka is a backend, not a dependency: the default in-process bus needs no broker.

## Source connectors

`server/src/sutr/source_connectors/` — one interface, seven verbs, one
normalization pipeline. The property to protect is the one the LLD states
directly: **the compiler never learns where a document came from**. A connector
turns a locator into raw text plus provenance and stops there; it does not
parse, validate, or normalize.

Four verbs are connector-specific and abstract. Three — `validate`,
`detect_drift`, `disconnect` — are implemented once on the base class, because a
document's validity has nothing to do with where it was found and drift is a
property of the API rather than the transport. Do not override them.

A source type that is named by the LLD but not built goes in
`registry.PLANNED` with its reason, and is served by
`GET /v1/sources/connectors` alongside the working ones. Silently missing looks
like an oversight; declared-and-refused is a decision.

## Drift

`openapi/diff.py` compares the **canonical IR**, never the document. A
reformatted file, a reordered key map, or a Swagger 2.0 → OpenAPI 3.x migration
must produce **no drift** — a report that fires on whitespace trains people to
dismiss drift reports, and then the one that matters gets dismissed too.

Two rules that are easy to get wrong:

- **Every security difference is SECURITY, in either direction.** Adding
  authentication breaks existing callers; removing it makes the API public.
- **Sync never compiles or deploys.** It updates the project's definition and
  stops. Compiling and deploying have their own gates, and a background loop
  must not reach through them (ADR-027).

## Documentation intelligence

`server/src/sutr/documentation/` — prose in, cited facts out:

    Document → Parse → Chunk → Extract → Graph → Embed

**Every extracted fact carries a citation, and this is not negotiable.** The
sentence verbatim, its section path, its page, its character offsets, the
confidence, and the version of the extractor that produced it. A business rule
with no provenance is an assertion the platform cannot defend, and the citation
is precisely what makes a wrong rule believable — so a rule that cannot cite
does not get stored.

Rules that follow from that, and are easy to get wrong:

- **Chunk by structure, never by size.** Fixed windows cut rules in half, and
  *"Refunds are allowed only within"* is not a rule. Partition into typed
  elements, group by heading path, and split a section on sentence boundaries
  only when it is too large to be one chunk.
- **The pipeline never touches the IR.** Documentation enriches through
  `document.project_id`, one way. `platform/boundaries.py` owns the table list,
  so a write in the other direction has to be declared before it can compile.
- **Only parsing can fail a job.** A graph backend that throws, an absent
  embedding provider, a page OCR cannot read — each records a *degradation* and
  the run continues as `partial`. Three states, not two (ADR-031): collapsing
  `partial` into `failed` makes people ignore real failures, and collapsing it
  into `processed` hides a document whose middle forty pages are missing.
- **Checkpoint after every stage.** `completed_stages_json` is the resume point;
  re-running a job must skip what already succeeded. Re-parsing a 300-page PDF
  because the graph step failed is absurd, and it is also what makes moving to a
  worker a change of caller rather than a rewrite (ADR-030).
- **A flattened table is not a sentence.** `extraction/layout.py` rejects lines
  that came out of a layout — a wide column gap, an arrow between boxes, a dot
  leader. Every extractor consults it before matching, because a rule citing
  two table cells that landed on the same line is a citation nobody can check,
  and that makes the *good* citations less believable too. On a real 31-page
  technical PDF this cut extracted terms from 54 to 16 and workflows from 76 to
  26, without changing what is extracted from any prose document.
- **Do not add a stand-in embedding provider.** A hash or TF-IDF vector packs,
  stores and cosines like a real one, and the search built on it looks semantic
  while behaving lexically — which is worse than not ranking at all (ADR-033).
  `GET /v1/documentation/search` says which mode it ran in; keep it that way.

Optional software (PDF libraries, Tesseract, an embedding provider) is
*reported*, never assumed: `GET /v1/documentation/capabilities` tells a caller
that a scanned PDF will come back empty before they upload it.

## MCP generation and runtime artifacts

`server/src/sutr/generation/` — IR + documentation knowledge + template in, one
immutable artifact out:

    knowledge → metadata → generate → validate → sign → store

**An artifact is never edited after it is written.** There is no PATCH route and
no service function that updates a `runtime_artifact` row. The whole validation
gate rests on this: an artifact that could be edited after passing would make
`validated` a statement about bytes that are no longer there.

Rules that follow, and are easy to get wrong:

- **`build_hash` is what enforces determinism.** It covers the inputs and the
  digests of the manifest and the package. Generating again from unchanged
  inputs must find the existing row — a second row would *mean* the build was
  not deterministic (ADR-036). So do not put a timestamp, a run id, or anything
  measured at build time into the manifest or the package.
- **`blocked` is not `ok`.** A check that could not run — no scanner, no
  vulnerability database — reports `blocked`, is named in the summary, and never
  counts as a pass (ADR-037). Adding a check that returns `ok` when it did
  nothing is the one change this module cannot survive.
- **Only validated artifacts deploy.** The gate lives in `api/deployments.py`,
  not in the generator, because a gate is only worth anything where it is
  enforced. A rejected artifact is still *stored*: validation failing is a
  finding, not a lost build.
- **"Signed" means one specific thing here.** An Ed25519 signature over the
  build hash, with a key the operator holds. Not Cosign, not an image, no
  transparency log (ADR-038). Do not let a docstring, a field name or a
  response say otherwise.
- **Documentation knowledge ships inside the package**, citations intact, and
  the tool it is attached to is a *match, not a claim*. An unmatched rule is
  still shipped at the top level; an unmatched workflow step stays unmatched
  rather than being linked to the nearest-looking operation (ADR-039).

Generated-package templates live beside the code that ships them:
`openapi/templates/sutr_metrics.py` is a real file read verbatim, the way
`runtime/request_builder.py` already was. Prefer that to a string constant —
code users read should be lintable where it is written. When you do edit an
inline template, remember it is a Python string: a `\n` in the generated file
needs `\\n` in the source, and getting that wrong produces a package that fails
to import rather than a test that fails to pass.

Bump `packaging.TEMPLATE_VERSION` when the generated code changes. It is
recorded on every artifact and is part of the build hash, which is what makes a
connector fix a new artifact even though the specification did not move.

Runtime isolation is the Kubernetes provider's job (ADR-040) and it is **NOT
TESTED against a live cluster**. Do not upgrade that label without a run
against a real API server.

## The registry, and the storefront derived from it

`server/src/sutr/registry/` is the authoritative record.
`server/src/sutr/marketplace/` is a projection of it. The dependency runs one
way and must keep doing so:

    registry → events → projection → storefront

**Nothing in `registry/` reads a listing.** A registry function that consulted
the storefront would make the derived thing an input to the record it is derived
from, and the two would start disagreeing in ways nobody could order.

**Nothing authorizes off a listing.** A listing can be one event behind.
Subscribing, deciding a change, checking entitlement — all re-read the registry.
A storefront row is for reading, and it says when it was projected so a reader
can see how current it is.

Rules that are easy to break by accident:

- **One lifecycle vocabulary**, in `registry/lifecycle.py`, and it is the LLD's
  fourteen-state machine from §3.1 (ADR-042). §3.7's five-state diagram is a
  summary of it, not a second machine. Adding a state means adding it there and
  nowhere else.
- **A projection handler rebuilds from the record**, using the event only as the
  signal. Patching a listing from an event payload makes every event carry a
  copy of the tool, and the copies drift.
- **Counted, not incremented.** Subscriber counts are recomputed on every
  projection. An increment is a number that goes wrong the first time an event
  is replayed.
- **An unmeasurable trust input abstains.** It leaves both sides of the
  fraction, and `coverage` reports what was measured (ADR-044). Never default a
  missing input to 0 — that is the same mistake as a null rendered as a zero,
  in the one number people will act on.
- **Prices and versions are immutable.** Changing a price supersedes a row and
  writes a new one; a version is never edited or deleted. Subscriptions and
  rollbacks point at them, and an edit would rewrite what somebody was sold.
- **`verified` is not settable by the provider**, and `regions`/`compliance` are
  labelled `declared_by_provider` in the response, because nothing has checked
  them.

Gated changes — entering review, publishing, visibility, pricing — create a
`RegistryChangeRequest` and change nothing until decided. Applying first and
asking later is not a gate.

## Discovery

`server/src/sutr/discovery/` turns an intent into a ranked recommendation. Two
structural properties, both worth keeping:

    intent → candidates → policy filter → hybrid retrieval → ranking → cache

- **It writes nothing.** No table, no migration. That is the enforceable form of
  the LLD's *"read-optimized"*, and `platform/boundaries.py` records the service
  with no tables at all.
- **It calls no provider.** §3.8 says discovery never invokes provider APIs, and
  nothing in the package does.

Rules that are easy to break:

- **Filter before rank.** Ranking is the expensive half; ranking something the
  caller cannot use wastes it, and a ranker that never sees an inaccessible
  candidate cannot leak it through a score. The stage list in the response is
  the evidence, and a test asserts the order.
- **Keep the exclusions.** When nothing survives the filter, the near misses come
  back as suggestions with the check that excluded each (LLD §4.2). An empty list
  teaches an agent nothing.
- **A check that cannot run says so.** `provider_policy` and `runtime_status` are
  declared with `enforced: false`. A check that silently never excludes anything
  is worse than a missing one — it looks like it ran.
- **A missing trust score is neutral, not zero** (ADR-048/ADR-044). Ranking is
  the one place where turning a null into a 0 would be most visible and least
  noticed.
- **Fusion by rank only when more than one ranker contributed.** RRF over a
  single list flattens first-vs-second to two thousandths and lets every other
  signal outvote relevance. `retrieval.fuse` handles both cases; do not simplify
  it back.
- **BM25 lives in `common/lexical.py`**, shared with documentation retrieval.
  One copy. The tokenizer folds plurals and nothing else — a real stemmer
  conflates words that mean different things (ADR-049).

Every response carries `degradations`, and an empty list is a claim that nothing
was skipped. If you add a way for a stage to be skipped, add its degradation and
its own test.

## Zero trust: identity, authorization, passes

`server/src/sutr/provisioning/` decides *who is calling* and *whether this call
is allowed*, and issues the credential that records the answer.

    principal → tenant → rbac → abac → (policy) → decision → pass

It is a **shared** service, not control-plane, and deliberately so: LLD §2.2
requires the data plane to keep deciding while the control plane is down, so the
decision point is a hot-path primitive. Authoring identities and rules is the
control-plane half and lives in `api/v1/provisioning.py`.

Rules that are easy to break:

- **The order is the design.** Each layer is cheaper than the next and refuses on
  less information. A cross-tenant caller must never cause a tenant's rule set to
  load, so tenant is layer 1 and the chain stops at the first denial. Layers that
  did not run are *absent* from the decision, never recorded as agreeing.
- **The four layer names are derived, not quoted.** The LLD's figure is an image
  (ADR-053). If you rename them, update `pdp.describe()['source']` too — the
  API's honesty about that derivation is part of the feature.
- **Deny wins; no matching rule is no opinion.** Priority orders *reporting*
  only. A tenant with no rules must keep the behaviour they had, or nobody turns
  the layer on.
- **Normalise both sides of every comparison**, and drop empty values. A rule
  saying `"India"` against an identity saying `"india"`, or a matcher that is
  `""`, is a rule that silently never matches — the worst failure this layer can
  have, and one that already happened once.
- **A pass is issued after a decision, never instead of one.** `passes.issue`
  raises on a refused decision. Asking for more tools than allowed **narrows**
  the pass; it does not widen it and does not refuse the whole request.
- **Never store the token.** The row holds claims and the decision. The JWT shape
  is a contract with every generated server already in the field — it validates
  it offline, which is also why it cannot see a revocation. Say that wherever
  revocation is mentioned.
- **A principal is optional on `CallContext`.** Without one, layers 1–3 are
  skipped and the call behaves exactly as it did before this existed. That is
  what made the layer safe to add to the hot path; keep it true.

`tests/test_provisioning/test_tenant_isolation.py` walks every tenant-scoped
`/v1` surface. Add a tenant-scoped resource, add it there.

## Governance

`server/src/sutr/governance/` is policy authoring, compliance, risk, the
approval workflow and exceptions. One module is **shared**, not control-plane:
`governance.policies`, because an ACTIVE version's rules are read on every
authorization decision and that is a hot-path primitive. Everything else is
control plane. Import it by full path (`from sutr.governance.policies import …`)
so the boundary analysis sees the shared half.

Rules that are easy to break:

- **Writing a policy is not deploying it.** Only an ACTIVE version's rules reach
  the decision point. A draft must change nothing, and a test pins that.
- **A version is immutable once it leaves DRAFT.** Everything downstream refers
  to what it said. Open a new draft instead.
- **Separation of duties is unconditional here** (ADR-058) — stricter than the
  registry's change gate on purpose, because a tenant with no policies loses
  nothing. Record both `authored_by` and `approved_by`; the rule is only
  checkable if both are kept.
- **Compliance does not certify.** Every control either names a check that reads
  a real fact, or says why it cannot be checked. A `manual` result is never a
  pass. If you add a control, add its check or its `manual_reason` — never a
  bare pass.
- **Risk is inverted.** Lower is better, the opposite of the registry's trust
  score, and nothing converts between them. An unmeasurable component abstains;
  a null score never auto-approves, because unknown risk is not low risk.
- **Every review stage names its evidence source.** A stage with no source is a
  stage somebody asserted, and a test asserts the source is present.
- **All exceptions expire.** `expires_at` is not nullable, capped at 90 days,
  and checked at decision time as well as by the sweep.

The four fail-safes (§5.2.11) are implemented where the failure occurs, and
`engine.describe()` lists each with its module. The audit one is deliberately
*stronger* than the LLD asks — same transaction, not a queue — and the ADR says
so rather than claiming compliance with a queue that does not exist.

`engine.POINTS` claims six of seven governance points and names monitoring as
ungated. If you gate it, update the entry; if you add a point, add its module.

## Billing, metering and money

`server/src/sutr/billing/` is pricing, the ledger, invoices, settlement and
dunning. It is control plane. `services/metering.py` is not part of it and must
not become part of it.

The rule everything else follows from: **metering records facts, billing decides
money, and the two never meet** (ADR-063).

- `record_usage` takes **no price parameter and writes none**, and
  `services/metering.py` must never import `sutr.billing`. Two tests enforce
  this — one inspects the signature, one reads the source. If you find yourself
  wanting a price on the execution path, read ADR-063 first; the answer is a
  published plan at billing time.
- **Money is integer micro-units, rates are basis points.** No float ever holds
  an amount. 1 000 000 micros is one unit of currency; 250 bps is 2.5%.
- **A missing amount is not zero.** Usage with no published plan lands in the
  invoice's `unpriced` array with a reason, rather than being billed at zero or
  dropped.
- **The ledger is append-only.** `ledger.py` has `append` and no update or
  delete, and a test reads the source to keep it that way. Corrections are
  compensating entries via `reverse()`, which requires a reason. Never delete a
  financial row — voiding an invoice reverses its entries and keeps everything.
- **Amounts are never negative**; `direction` carries the sign.
- **Plans are immutable once published.** A price change is a new version that
  supersedes the old one, so an invoice can name the exact version each line
  used. Validate at publish time, not at evaluation time.
- **The revenue share is never hard-coded** (build prompt §44).
  `settlement.default_share_bps()` reads settings on every call, a run may
  override it, and whatever applied is written onto the settlement row. A test
  asserts the module source contains no literal split.
- **Nothing here moves money.** `record_payment` and `mark_paid_out` record acts
  performed elsewhere. Every response says so in a field —
  `collected_by_this_platform`, `paid_out_by_this_platform`, dunning's
  `scheduler: null` — and `GET /v1/metering/capabilities` says all of it in one
  place. Do not add a route whose name implies collection.

`registry_pricing` (Phase 6) is what a listing *advertises*; `pricing_plan` is
what an invoice is *computed from*. They are deliberately separate (ADR-068).

## The console

`ui/src/pages/v1/` is one page per platform service, over `ui/src/api/v1.ts`.
Four rules, each learned the hard way:

- **`npx tsc --noEmit` checks nothing.** The root `tsconfig.json` is
  `{"files": []}` with project references, so the bare command type-checks an
  empty program and exits 0. The real one is
  `npx tsc -p tsconfig.app.json --noEmit`.
- **Never assume a collection's shape.** Some `/v1` routes answer with a bare
  array and others name the collection alongside their own fields. `listOf()`
  in `api/v1.ts` accepts either and throws on neither — a page that guesses
  wrong renders an empty table, which is indistinguishable from "you have
  none".
- **Tabs go in `SutrPageHeader`, not `SutrPageBody`.** In the body they render
  correctly and cannot be clicked: the page body intercepts the pointer.
- **Take field names from a running server.** `lifecycle_state`, not `state`;
  an agent's `kind`, not `agent_type`; a pass's `resource` and `tools`, not
  `tool_id`.

Carry the platform's honesty into the display: a null score reads *not scored*,
an unavailable trust component reads *unavailable* rather than zero, an invoice
says it was not collected, and a per-replica view says so on the page.

For local work, `SUTR_API_TARGET=http://localhost:8000 pnpm dev` points the
proxy at your backend instead of whatever is on port 4747.

## When something fails

LLD §5.8. The code is in `server/src/sutr/resilience/`.

- **Only count failures that describe the provider** (ADR-078). A 404 is a
  provider working correctly and disagreeing with the request; opening a
  circuit on a run of them takes a healthy provider away from every caller. A
  5xx counts. A refusal by this platform — an unsafe URL, a body too large —
  never does: the call did not leave the process.
- **A refused call raises; it does not return an error result.** Returning one
  records an execution that never happened: metered, logged as `executed`, and
  counted against a provider that was not called.
- **Retry only what did not arrive, and only where repeating is safe**
  (ADR-079). Transport failures on GET/HEAD/OPTIONS. Not a 500 — that response
  reached the provider's application, and we do not know whether that tool was
  idempotent.
- **Backoff has full jitter.** Without it, everything that failed at the same
  moment retries at the same moment.

If you add a failure mode, add it to `resilience/domains.py` — and fill in the
column that matters, which is not what breaks but **what keeps working**.

## Security gates, and the rule about evidence

Every gate in `.github/workflows/security.yml` has been executed and every
finding fixed or accepted with a written reason (`docs/SECURITY_GATES.md`).
Two rules follow:

- **Fix by preference, accept by exception.** An acceptance is recorded twice —
  a `nosemgrep` or allowlist entry at the site, and a row in
  `docs/SECURITY_GATES.md`. Neither can drift without the other becoming
  obviously wrong.
- **Pin everything.** Actions are pinned to commit SHAs with the version in a
  trailing comment; images are pinned to exact tags. A mutable tag means two
  runs of "the same" build are free to run different code.

Load and chaos results live in `qa/` and **always name the machine**. A number
without the machine it was taken on is not a result, and none of the LLD's
performance targets has been measured (ADR-081).

## Running more than one replica

LLD §5.4/§5.7/§5.8. Four rules, each with a test behind it.

- **Work that must run once goes under a lease** (ADR-074). If you add a
  background loop, decide which it is: per-replica work (warming this
  process's cache) runs everywhere; anything that writes, meters or publishes
  gets a job name in `platform/leadership.py` and runs under
  `run_as_singleton`. The deployment sweep meters runtime minutes — running it
  twice bills a tenant twice, and nothing would have told you.
- **A standby must be able to refuse your endpoint.** A write served by a
  read-only replica is a 500 the caller cannot act on. New POST routes are
  refused on a standby by default; if yours genuinely writes nothing, add its
  exact path to `mode.READ_ONLY_WRITES` — the tests check the path is a real
  route and that the handler contains no `session.commit()`.
- **Liveness checks the process; readiness checks the database** (ADR-076).
  Never make `/health/live` depend on anything external: a liveness probe that
  fails during a database outage restarts every replica at once.
- **Process-local state gets declared, not hidden.** A new in-memory cache,
  counter or waiter set goes in `platform/scaling.py` with what changes at N
  replicas. A test asserts every entry still names an attribute that exists.

Two operational facts worth knowing before you debug them: every replica needs
the *same* `JWT_SECRET_KEY` (otherwise sessions flap), and
`DB_STATEMENT_TIMEOUT_MS` must not be set behind PgBouncer (it is a libpq
startup parameter, and PgBouncer refuses unknown ones — the replica will not
boot). Put the timeout on the database role instead.

Infrastructure is shipped only for stores this build actually opens a
connection to (ADR-077). Do not add a chart for a database nothing talks to.

## Telemetry: traces, metrics, logs

LLD §5.3. Four rules, each with a test behind it.

- **A span carries shape, never content** (ADR-070). No tool arguments, no
  results, no URLs (a credential can be in a query string — ADR-009), no search
  intents. Host, method, tool name, outcome, duration, and the correlation id.
  If you want to know *what* was called, that is the tenant's own call log,
  behind a permission check.
- **Trace context propagates both ways** (ADR-069). Inbound `traceparent` is
  continued rather than replaced; outbound calls carry ours.
  `observability/propagation.py` is the only place that does either. The
  correlation id goes out with or without the OpenTelemetry SDK; the trace
  context goes only when a span is recording.
- **`/metrics` never labels a tenant.** Not org, not tenant, not user, not key —
  a test walks every exported series. Per-tenant numbers belong to
  `/v1/observability`, which reads this platform's own tables scoped by the
  query (ADR-071). Adding a tenant label is not a cardinality trade-off to
  weigh; it is a disclosure.
- **A gauge that could not be sampled is stale, not zero** (ADR-072). The
  scrape samples queue depth and lag from the database; a failure counts itself
  in `sutr_telemetry_sample_failures_total` and leaves the last values in
  place. Zero is a number an alert acts on.

If you add a metric, add its panel or alert in `deploy/observability/` — and if
you rename one, the asset tests will tell you which dashboard you just broke.
An alert must name a runbook section in `RUNBOOKS.md`; the same tests check the
link goes somewhere.

## Honesty rules that are enforced by tests

Three of these come up constantly and each is pinned by a test, because each one is easy to break
by accident:

- **A missing number is `null`, never `0`.** A marketplace listing's `trust_score` is null because
  the Registry does not exist, not zero. A remote MCP server's `tool_count` is null because its
  tools live upstream. A cloud deployment's `cpu_percent` is null with an `unavailable_reason`
  naming the monitoring API it would need.
- **A capability that cannot be enforced says so.** `GOVERNANCE_MODE=platform` refuses to start
  without the material to verify an access pass. A token quota that nothing measures is reported
  `enforceable: false` rather than silently passing.
- **An undocumented external API is BLOCKED, not guessed.** See the Swaraj Cloud row above.

The ESDS evolution's design records live in `docs/ARCHITECTURE_DECISIONS.md`,
`docs/REFERENCE_MAP.md`, `docs/ESDS_IMPLEMENTATION_GAP.md` and
`docs/ESDS_TRACEABILITY_MATRIX.md`. Read the relevant ADR before changing something it covers.

## Documentation (`docs/`)

The `docs/` directory is a [teeny](https://github.com/yakkomajuri/teeny) static site — it's the source of truth for anything a developer or agent needs to understand the system beyond the code itself. **Keep it updated** — if you add an endpoint, change a data model, or introduce a new integration type, update the relevant doc file in the same PR/commit.

Layout:
- `docs/pages/` — one Markdown file per page; filename becomes the URL slug (`pages/api.md` → `/api`).
- `docs/templates/default.html` — the HTML shell (sidebar + content) that wraps every page.
- `docs/static/` — CSS and other static assets.
- `docs/teeny.config.js`, `docs/package.json` — site config and `teeny-cli` dev dependency.

Current pages:
- `pages/api.md` — full REST API reference
- `pages/mcp-server.md` — `/mcp` aggregation endpoint
- `pages/tool-approvals.md` — tool approval/policy flow
- `pages/google-oauth-setup.md` — shared Google OAuth app setup

To add a page: drop a new `.md` file in `pages/` and add a link to the sidebar in `templates/default.html`. Run `pnpm dev` from `docs/` for a local hot-reload server, or `pnpm build` to produce static output in `public/`.

## Project Layout

```
sutr/
├── AGENTS.md           ← this file
├── CLAUDE.md           ← symlink to AGENTS.md
├── docs/               ← documentation (keep up to date — see above)
├── server/             ← Python / FastAPI
│   ├── pyproject.toml
│   └── src/sutr/
│       ├── api/        ← one file per resource (+ api/v1/ for the /v1 surface)
│       ├── models/     ← SQLModel tables
│       ├── mcp/        ← MCP gateway: server + streamable HTTP, sse.py, stdio.py, client
│       ├── common/     ← errors, responses, pagination, idempotency (shared)
│       ├── events/     ← outbox, bus, relay, idempotent consumers
│       ├── platform/   ← service boundaries and plane map (enforced by tests)
│       ├── runtime/    ← request_builder.py — THE definition of how a tool becomes a request
│       ├── integrations/
│       │   ├── bundled/    ← one file per integration
│       │   └── categories.py ← marketplace taxonomy for all of them, in one place
│       ├── openapi/    ← spec → IR → tools (pipeline, diff, fingerprint, linting, swagger2)
│       │   └── templates/ ← files copied verbatim into every generated package
│       ├── generation/ ← IR + knowledge + template → validated, signed, immutable artifacts
│       ├── registry/   ← the system of record: lifecycle, versions, pricing, trust
│       ├── marketplace/← the storefront projected from registry events
│       ├── discovery/  ← intent → policy-filtered, ranked tools (writes nothing)
│       ├── provisioning/ ← identity, the four authorization layers, scoped passes
│       ├── governance/  ← versioned policy, compliance, risk, review, exceptions
│       ├── billing/     ← pricing, the append-only ledger, invoices, settlement, dunning
│       ├── source_connectors/ ← where definitions live: 7 connectors, 7 verbs
│       ├── documentation/ ← prose → cited rules, workflows, glossary, graph, search
│       ├── credentials/← per-scheme credentials for compiled APIs (ADR-009)
│       ├── connections/← connected accounts (OAuth grants for sources + deploy targets)
│       ├── observability/ ← metrics, traces, structured JSON logging, tenant telemetry
│       ├── services/   ← quota.py, marketplace.py, deployments.py, tool_pipeline.py, …
│       └── deploy/     ← providers: docker, gcp, azure, aws/, kubernetes, swaraj (blocked)
└── cli/                ← TypeScript / Commander.js
    ├── package.json
    └── src/
        └── commands/   ← one file per command group
```
