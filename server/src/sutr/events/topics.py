"""The topic catalog and its partitioning rules (ESDS LLD §3.1, §5.6).

Every event type the platform will emit is named here, whether or not anything
emits it yet. Two reasons: the names become a contract the moment the first one
is published, and a single list is the only way to see the pipeline's shape
without reading fifteen services.

Partitioning follows the LLD exactly — `provider.*` by `provider_id`, `tool.*`
by `tool_id`, `runtime.*` by `runtime_id`, invoices by `invoice_id`, policies
by `policy_id` — because ordering is guaranteed *per partition*, and the entity
a fact is about is the thing whose facts must stay in order.
"""

from dataclasses import dataclass

# ── Event type names ─────────────────────────────────────────────────────────

PROVIDER_CREATED = "provider.created"
PROVIDER_UPDATED = "provider.updated"

SOURCE_CONNECTED = "source.connected"
SOURCE_CHANGED = "source.changed"
SOURCE_DRIFT_DETECTED = "source.drift_detected"

API_UPLOADED = "api.uploaded"

TRANSLATION_STARTED = "translation.started"
TRANSLATION_COMPLETED = "translation.completed"
TRANSLATION_FAILED = "translation.failed"

DOCUMENTATION_UPLOADED = "documentation.uploaded"
PARSING_COMPLETED = "parsing.completed"
KNOWLEDGE_EXTRACTED = "knowledge.extracted"
EMBEDDINGS_GENERATED = "embeddings.generated"
DOCUMENTATION_PROCESSED = "documentation.processed"
DOCUMENTATION_FAILED = "documentation.failed"

METADATA_GENERATED = "metadata.generated"

GENERATION_STARTED = "generation.started"
MCP_GENERATED = "mcp.generated"
GENERATION_FAILED = "generation.failed"
VALIDATION_COMPLETED = "validation.completed"

RUNTIME_DEPLOYED = "runtime.deployed"
RUNTIME_UPDATED = "runtime.updated"
RUNTIME_ROLLED_BACK = "runtime.rolled_back"
RUNTIME_FAILED = "runtime.failed"
RUNTIME_INVOKED = "runtime.invoked"

TOOL_REGISTERED = "tool.registered"
TOOL_UPDATED = "tool.updated"
TOOL_APPROVED = "tool.approved"
TOOL_PUBLISHED = "tool.published"
TOOL_DEPRECATED = "tool.deprecated"
TOOL_ARCHIVED = "tool.archived"
VERSION_CREATED = "version.created"

SUBSCRIPTION_CREATED = "subscription.created"
REVIEW_CREATED = "review.created"
RATING_UPDATED = "rating.updated"
PRICING_UPDATED = "pricing.updated"

USAGE_RECORDED = "usage.recorded"
INVOICE_GENERATED = "invoice.generated"

POLICY_UPDATED = "policy.updated"

ALL_TOPICS: tuple[str, ...] = (
    PROVIDER_CREATED,
    PROVIDER_UPDATED,
    SOURCE_CONNECTED,
    SOURCE_CHANGED,
    SOURCE_DRIFT_DETECTED,
    API_UPLOADED,
    TRANSLATION_STARTED,
    TRANSLATION_COMPLETED,
    TRANSLATION_FAILED,
    DOCUMENTATION_UPLOADED,
    PARSING_COMPLETED,
    KNOWLEDGE_EXTRACTED,
    EMBEDDINGS_GENERATED,
    DOCUMENTATION_PROCESSED,
    DOCUMENTATION_FAILED,
    METADATA_GENERATED,
    GENERATION_STARTED,
    MCP_GENERATED,
    GENERATION_FAILED,
    VALIDATION_COMPLETED,
    RUNTIME_DEPLOYED,
    RUNTIME_UPDATED,
    RUNTIME_ROLLED_BACK,
    RUNTIME_FAILED,
    RUNTIME_INVOKED,
    TOOL_REGISTERED,
    TOOL_UPDATED,
    TOOL_APPROVED,
    TOOL_PUBLISHED,
    TOOL_DEPRECATED,
    TOOL_ARCHIVED,
    VERSION_CREATED,
    SUBSCRIPTION_CREATED,
    REVIEW_CREATED,
    RATING_UPDATED,
    PRICING_UPDATED,
    USAGE_RECORDED,
    INVOICE_GENERATED,
    POLICY_UPDATED,
)


@dataclass(frozen=True)
class TopicSpec:
    """How one family of events is carried."""

    prefix: str
    # Which id orders this family. Ordering is per partition, so the entity a
    # fact is about is the entity whose facts stay in order.
    partition_by: str
    description: str


# Longest prefix wins, so `runtime.invoked` and `runtime.deployed` can differ
# if they ever need to.
TOPIC_SPECS: tuple[TopicSpec, ...] = (
    TopicSpec("provider.", "provider_id", "Provider lifecycle."),
    TopicSpec("source.", "provider_id", "Source connection, change and drift."),
    TopicSpec("api.", "provider_id", "Specification uploads."),
    TopicSpec("translation.", "provider_id", "Specification → IR."),
    TopicSpec("documentation.", "provider_id", "Document ingestion and enrichment."),
    TopicSpec("parsing.", "provider_id", "Document parsing stages."),
    TopicSpec("knowledge.", "provider_id", "Knowledge extraction."),
    TopicSpec("embeddings.", "provider_id", "Embedding generation."),
    TopicSpec("metadata.", "tool_id", "AI metadata generation."),
    TopicSpec("generation.", "tool_id", "MCP server generation."),
    TopicSpec("mcp.", "tool_id", "Generated MCP artifacts."),
    TopicSpec("validation.", "tool_id", "Artifact validation."),
    TopicSpec("runtime.", "runtime_id", "Runtime lifecycle and invocation."),
    TopicSpec("tool.", "tool_id", "Registry tool lifecycle."),
    TopicSpec("version.", "tool_id", "Immutable version creation."),
    TopicSpec("subscription.", "tool_id", "Marketplace subscriptions."),
    TopicSpec("review.", "tool_id", "Marketplace reviews."),
    TopicSpec("rating.", "tool_id", "Marketplace ratings."),
    TopicSpec("pricing.", "tool_id", "Pricing changes."),
    TopicSpec("usage.", "tenant_id", "Metering facts."),
    TopicSpec("invoice.", "invoice_id", "Billing documents."),
    TopicSpec("policy.", "policy_id", "Governance policy changes."),
)


def spec_for(event_type: str) -> TopicSpec | None:
    matches = [spec for spec in TOPIC_SPECS if event_type.startswith(spec.prefix)]
    if not matches:
        return None
    return max(matches, key=lambda spec: len(spec.prefix))


def partition_key(event_type: str, *, resource_id: str | None, tenant_id: str | None) -> str:
    """The key that keeps one entity's facts in order.

    Falls back to the tenant, then to the event type: an event with no key at
    all would be spread across partitions and lose ordering entirely, which is
    worse than coarse ordering.
    """
    spec = spec_for(event_type)
    if spec is None:
        return tenant_id or event_type
    if spec.partition_by == "tenant_id":
        return tenant_id or resource_id or event_type
    return resource_id or tenant_id or event_type


def topic_for(event_type: str) -> str:
    """The transport topic an event type is published to.

    One topic per event type. The LLD's catalog names topics and event types
    identically, and a shared topic would force every consumer of one type to
    filter out the others.
    """
    return event_type
