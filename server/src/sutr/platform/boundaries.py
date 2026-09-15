"""The service map, the plane split, and table ownership.

The ESDS LLD names 15 control-plane services (§2.3), requires the data plane to
keep working when the control plane is down (§2.2), and mandates
database-per-service (§2.4). Sutr is one process with one database, and the
build prompt is explicit that it should stay that way for now: *"Initially
these may remain inside the Sutr monorepo as modular services. DO NOT
prematurely split them into 15 deployments. Create clear package/service
boundaries first."*

This module is those boundaries. It is a declaration, and
`tests/test_platform/` turns the declaration into three enforced rules:

1. **Every module belongs to exactly one service.** A module nobody owns is a
   module nobody will maintain, and adding one fails the build.
2. **Every table is written by exactly one service.** This is what
   database-per-service actually buys — clear ownership — expressed in the form
   a single database can hold (ADR-001). Other services read through the owner's
   repository, never by joining to its tables.
3. **Data-plane modules do not import control-plane modules.** This is the
   testable form of "existing tools keep working when the control plane is
   down" (ADR-002). A hot-path module that imports the registry has taken a
   dependency on a service that is allowed to be offline.

Rule 3 is the one with teeth. The other two are bookkeeping that stops the map
rotting; rule 3 is an architectural guarantee that a single careless import
would otherwise erase silently.
"""

from dataclasses import dataclass, field
from enum import Enum


class Plane(str, Enum):
    """Which half of the platform a service belongs to.

    SHARED is for infrastructure both planes need — configuration, the database
    session, telemetry. Shared modules may be imported by anyone, and must
    themselves import neither plane.
    """

    CONTROL = "control"
    DATA = "data"
    SHARED = "shared"


@dataclass(frozen=True)
class Service:
    """One logical service, as the LLD names it."""

    name: str
    plane: Plane
    description: str
    # Modules under `sutr.` that make up this service, by dotted prefix.
    modules: tuple[str, ...] = ()
    # Tables this service *writes*. Exactly one service writes any table.
    tables: tuple[str, ...] = ()


# ── Shared infrastructure ────────────────────────────────────────────────────

SHARED = (
    Service(
        name="common",
        plane=Plane.SHARED,
        description="Errors, response envelope, pagination, idempotency (LLD §2.6).",
        modules=("common",),
        tables=("idempotency_key",),
    ),
    Service(
        name="database",
        plane=Plane.SHARED,
        description="Engine, sessions, migrations.",
        modules=("db",),
    ),
    Service(
        name="events",
        plane=Plane.SHARED,
        description="Outbox, bus, relay, idempotent consumers (LLD §5.6).",
        modules=("events", "api.v1.events"),
        tables=("outbox_event", "consumed_event"),
    ),
    Service(
        name="telemetry",
        plane=Plane.SHARED,
        description="Metrics, tracing, structured logging (LLD §5.3).",
        modules=(
            "observability",
            "request_context",
            "analytics",
            "services.redaction",
            "api.v1.observability",
        ),
    ),
    Service(
        name="platform",
        plane=Plane.SHARED,
        description=(
            "Service boundaries, capability reporting, liveness and readiness, and which "
            "replica runs the work that must run once (LLD §5.4, §5.7)."
        ),
        modules=(
            "platform",
            "config",
            "api.v1",
            "api.v1.platform",
            "api.health",
            # The failure-handling primitives themselves are hot-path shared
            # code. The API that reads and resets them is not: closing a
            # circuit by hand is an audited operator action, and audit is
            # governance's table — so the router lives in the control plane
            # (see the governance service below).
            "resilience",
        ),
        tables=("leader_lease",),
    ),
    Service(
        name="contracts",
        plane=Plane.SHARED,
        description="Shared models and types every service reads.",
        modules=("models", "integrations.types"),
    ),
    Service(
        name="identity_verification",
        plane=Plane.SHARED,
        description=(
            "Verifying who is calling. The LLD's data-plane lifecycle (§3.2) begins with "
            "Authentication and Tenant Resolution, so verification is a hot-path primitive "
            "even though identity *management* is a control-plane service."
        ),
        modules=(
            "dependencies",
            "authz",
            "auth_tokens",
            "token_auth",
            "security",
            "secrets",
        ),
    ),
    Service(
        name="policy_evaluation",
        plane=Plane.SHARED,
        description=(
            "The PDP/PEP primitive: who is calling, and whether this call is allowed. LLD §5.2 "
            "separates the decision point from enforcement, and enforcement is on the hot path "
            "— §2.2 requires the data plane to keep deciding while the control plane is down. "
            "Authoring identities, rules and policies is governance and lives in the control "
            "plane; evaluating them is here."
        ),
        modules=(
            "approvals.policy",
            "approvals.normalize",
            "provisioning",
            # Policy *evaluation input*: an ACTIVE version's rules are read on
            # every authorization decision, so it is a hot-path primitive by
            # the same argument as the rest of this service. Everything else
            # under `governance.` is control plane.
            "governance.policies",
        ),
        tables=(
            "agent_identity",
            "access_pass",
            "access_rule",
            "governance_policy",
            "governance_policy_version",
        ),
    ),
    Service(
        name="metering",
        plane=Plane.SHARED,
        description=(
            "Writing execution facts. LLD §5.1: metering captures facts only and prices are "
            "evaluated at billing time, so the write is hot-path and the aggregation is not."
        ),
        modules=("services.metering",),
        tables=("usage_event",),
    ),
)

# ── Control plane: manages the tool lifecycle ────────────────────────────────

CONTROL_PLANE = (
    Service(
        name="identity",
        plane=Plane.CONTROL,
        description="AuthN/AuthZ: OAuth2, OIDC, JWT, RBAC, MFA (LLD §2.3).",
        modules=(
            "auth_start",
            "totp",
            "api.user_auth",
            "api.users",
            "api.auth",
            "api.api_keys",
            "api.totp",
            "api.second_factor",
            "api.password_change",
            "api.password_reset",
            "api.email_verification",
            "api.google_login",
            "api.oauth_server",
            "api.orgs",
            "api.workspaces",
            "api.org_settings",
            "api.admin",
            "api.config",
            "api.schemas",
            "email",
            "connections",
            "api.connections",
            "credentials.store",
            "api.integration_credentials",
        ),
        tables=(
            "user",
            "org",
            "org_membership",
            "org_invitation",
            "workspace",
            "api_key",
            "secret",
            "oauth_state",
            "oauth_client",
            "oauth_auth_code",
            "oauth_auth_request",
            "oauth_connect_state",
            "oauth_revoked_token",
            "google_login_state",
            "provider_connection",
            "integration_credential",
            "instance_settings",
            "waitlist",
        ),
    ),
    Service(
        name="source_connectors",
        plane=Plane.CONTROL,
        description="Ingest API definitions from files, URLs, Git, registries (LLD §3.3).",
        modules=(
            "openapi.sources",
            "openapi.loader",
            "source_connectors",
            "services.source_sync",
            "services.source_sync_loop",
            "api.v1.sources",
        ),
        tables=("api_source", "drift_report"),
    ),
    Service(
        name="documentation_intelligence",
        plane=Plane.CONTROL,
        description=(
            "Documents \u2192 business rules, workflows, glossary, knowledge graph "
            "and retrieval (LLD \u00a73.5)."
        ),
        modules=("documentation", "api.v1.documentation"),
        tables=(
            "document",
            "document_job",
            "document_chunk",
            "business_rule",
            "doc_workflow",
            "glossary_term",
            "knowledge_node",
            "knowledge_edge",
            "chunk_embedding",
        ),
    ),
    Service(
        name="translation",
        plane=Plane.CONTROL,
        description="Specification → canonical IR (LLD §3.4).",
        modules=(
            "openapi.normalizer",
            "openapi.resolver",
            "openapi.swagger2",
            "openapi.security",
            "openapi.errors",
            "openapi.limits",
            "openapi.linting",
            "openapi.pipeline",
            "openapi.diff",
            "openapi.fingerprint",
            "api.openapi_lint",
        ),
    ),
    Service(
        name="mcp_generator",
        plane=Plane.CONTROL,
        description=(
            "IR + documentation knowledge + template → validated, signed, immutable "
            "runtime artifacts (LLD §3.6)."
        ),
        modules=(
            "openapi.compiler",
            "openapi.packaging",
            # Template files copied verbatim into generated packages. They are
            # never imported by the platform; they are shipped by it.
            "openapi.templates",
            "api.openapi_projects",
            "generation",
            "api.v1.generation",
        ),
        tables=("openapi_project", "runtime_artifact"),
    ),
    Service(
        name="runtime_manager",
        plane=Plane.CONTROL,
        description="Deploy and manage hosted runtimes (LLD §3.2, §2.3).",
        modules=("deploy", "services.deployments", "api.deployments"),
        tables=("deployment", "deployment_revision"),
    ),
    Service(
        name="registry",
        plane=Plane.CONTROL,
        description=(
            "The authoritative system of record: tool metadata, immutable versions, "
            "governance state, pricing and trust (LLD §3.7)."
        ),
        modules=(
            "registry",
            "api.v1.registry",
            "integrations.registry",
            "integrations.bundled",
            "api.integrations",
            "mcp.management_tools",
        ),
        tables=(
            "registry_tool",
            "registry_version",
            "registry_change_request",
            "registry_pricing",
            "custom_api_integration",
            "custom_mcp_integration",
        ),
    ),
    Service(
        name="provisioning",
        plane=Plane.CONTROL,
        description=(
            "Authoring agent identities and access rules, and issuing scoped access passes "
            "(LLD §2.3, §4.3). The evaluation these rest on is a shared hot-path primitive; "
            "this is the surface that writes them."
        ),
        modules=("api.v1.provisioning",),
    ),
    Service(
        name="discovery",
        plane=Plane.CONTROL,
        description=(
            "Natural-language intent → a ranked, policy-filtered tool. Read-only: it writes "
            "no table and calls no provider API (LLD §3.8)."
        ),
        modules=("discovery", "api.v1.discovery"),
    ),
    Service(
        name="marketplace",
        plane=Plane.CONTROL,
        description=(
            "The read-optimized storefront derived from registry events, plus the "
            "subscriptions and provider profiles that are its own (LLD §3.7)."
        ),
        modules=(
            "marketplace",
            "api.v1.marketplace",
            "services.marketplace",
            "api.marketplace",
            "integrations.categories",
            "api.custom_api",
            "api.custom_mcp",
            "api.installed",
        ),
        tables=(
            "marketplace_listing",
            "tool_subscription",
            "provider_profile",
            "marketplace_review",
            "installed_integration",
        ),
    ),
    Service(
        name="governance",
        plane=Plane.CONTROL,
        description=(
            "Policy authoring and lifecycle, compliance, risk, the approval workflow, "
            "exceptions, approvals and audit (LLD §5.2)."
        ),
        modules=(
            "approvals",
            "governance",
            "api.v1.governance",
            "services.audit",
            "api.tool_approvals",
            "api.tool_settings",
            "api.audit",
            "api.logs",
            # Reading circuit state and closing one by hand: an operator action
            # on shared state, recorded in governance's audit trail. The
            # breaker itself is shared hot-path code (see `telemetry`/`platform`
            # above); only its API is control plane.
            "api.v1.resilience",
        ),
        tables=(
            "tool_execution_setting",
            "tool_approval_request",
            "audit_event",
            "log_entry",
            "compliance_run",
            "risk_assessment",
            "governance_review",
            "governance_exception",
        ),
    ),
    Service(
        name="billing",
        plane=Plane.CONTROL,
        description="Usage aggregation, quotas, invoices, settlement (LLD §5.1).",
        modules=(
            "billing",
            "api.billing",
            "api.usage",
            "api.quotas",
            "api.v1.billing",
        ),
        tables=(
            "subscription",
            "processed_stripe_event",
            "quota",
            "pricing_plan",
            "invoice",
            "invoice_line",
            "ledger_entry",
            "settlement",
            "payment_attempt",
        ),
    ),
)

# ── Data plane: executes live agent requests ────────────────────────────────

DATA_PLANE = (
    Service(
        name="gateway",
        plane=Plane.DATA,
        description="The agent-facing MCP and REST surfaces (LLD §3.2).",
        modules=(
            "mcp",
            "mcp.server",
            "mcp.asgi",
            "mcp.sse",
            "mcp.stdio",
            "mcp.client",
            "mcp.notifications",
            "mcp.refresh",
            "mcp.oauth",
            "mcp.oauth_provider",
            "api.tools",
        ),
    ),
    Service(
        name="runtime_executor",
        plane=Plane.DATA,
        description="Stateless execution: resolve, dispatch, serialize (LLD §3.2).",
        modules=(
            "runtime",
            "api_client",
            "services.tool_pipeline",
            "services.tool_catalog",
            "services.quota",
            "credentials.resolve",
            "credentials.read",
        ),
        tables=("tool_cache",),
    ),
    Service(
        name="rate_limiter",
        plane=Plane.DATA,
        description="Request admission control on the hot path.",
        modules=("rate_limit", "upstream_safety"),
    ),
)

ALL_SERVICES: tuple[Service, ...] = SHARED + CONTROL_PLANE + DATA_PLANE

# Modules that are the composition root or an entry point: they wire every
# service together and therefore belong to none of them.
COMPOSITION_ROOT: tuple[str, ...] = (
    "main",
    "api",
    # Namespace packages with no code of their own.
    "services",
    "openapi",
    "integrations",
    "credentials",
    # The background scheduler drives every service's periodic work, so like
    # `main` it is wiring rather than a service.
    "maintenance",
)


@dataclass
class Boundary:
    """Resolved lookups, built once."""

    by_module: dict[str, Service] = field(default_factory=dict)
    by_table: dict[str, Service] = field(default_factory=dict)


def _build() -> Boundary:
    boundary = Boundary()
    for service in ALL_SERVICES:
        for module in service.modules:
            if module in boundary.by_module:
                raise ValueError(
                    f"module {module!r} is claimed by both "
                    f"{boundary.by_module[module].name!r} and {service.name!r}"
                )
            boundary.by_module[module] = service
        for table in service.tables:
            if table in boundary.by_table:
                raise ValueError(
                    f"table {table!r} is owned by both "
                    f"{boundary.by_table[table].name!r} and {service.name!r}"
                )
            boundary.by_table[table] = service
    return boundary


BOUNDARY = _build()


def service_for_module(module: str) -> Service | None:
    """The service owning a dotted module path, by longest matching prefix.

    `module` is relative to `sutr.` — e.g. `services.tool_pipeline`.
    """
    parts = module.split(".")
    for length in range(len(parts), 0, -1):
        candidate = ".".join(parts[:length])
        if candidate in BOUNDARY.by_module:
            return BOUNDARY.by_module[candidate]
    return None


def service_for_table(table: str) -> Service | None:
    return BOUNDARY.by_table.get(table)


def plane_for_module(module: str) -> Plane | None:
    service = service_for_module(module)
    return service.plane if service else None


def services_in(plane: Plane) -> tuple[Service, ...]:
    return tuple(service for service in ALL_SERVICES if service.plane is plane)


def is_composition_root(module: str) -> bool:
    """True for the wiring modules, which are allowed to import everything."""
    return module in COMPOSITION_ROOT


# ── Known plane violations: the debt, named ─────────────────────────────────
#
# Sutr was built as one plane, and the LLD's §2.2 guarantee — the data plane
# keeps working when the control plane is down — is not true yet. The Phase 0
# audit says so, and pretending otherwise by classifying the offenders as
# "shared" until the test passes would turn an honest gap into a false claim.
#
# So the rule is a ratchet instead: these imports are permitted, everything
# else is not. The list may shrink and must never grow. Each entry names the
# phase that removes it.
KNOWN_PLANE_VIOLATIONS: dict[tuple[str, str], str] = {
    (
        "mcp.client",
        "integrations.registry",
    ): (
        "The gateway resolves a tool by reading the registry directly. The LLD's Runtime Router "
        "(§3.2) resolves from a cache instead, so an unavailable registry cannot stop an already "
        "provisioned tool. Removed by the Runtime Router in Phase 7."
    ),
    (
        "mcp.server",
        "approvals",
    ): (
        "The await-approval long-poll consumes an approval grant on the hot path. Under PDP/PEP "
        "(§5.2) the hot path asks for a decision and does not manage request lifecycle. Removed "
        "in Phase 9."
    ),
    (
        "mcp.server",
        "approvals.requests",
    ): (
        "Same as above: `mcp.server` consumes and resolves approval requests inline. Removed in "
        "Phase 9."
    ),
    (
        "services.tool_pipeline",
        "approvals.requests",
    ): (
        "The execution pipeline creates and consumes approval requests itself. A PEP should "
        "receive a decision, not write the governance record. Removed in Phase 9."
    ),
}


def is_known_violation(module: str, imported: str) -> bool:
    return (module, imported) in KNOWN_PLANE_VIOLATIONS
