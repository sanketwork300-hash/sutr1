import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint

# ── The lifecycle vocabulary (ESDS LLD §3.1, p. 11) ──────────────────────────
#
# The LLD calls this the "tool lifecycle state machine (shared vocabulary for
# the whole platform)" and lists fourteen states in order. §3.7's diagram shows
# five of them (DRAFT → VALIDATED → DEPLOYED → UNDER_REVIEW → PUBLISHED); that
# is a summary of the same machine, not a second one, so there is one vocabulary
# here rather than two that would have to be kept in step.
DRAFT = "DRAFT"
API_UPLOADED = "API_UPLOADED"
TRANSLATING = "TRANSLATING"
IR_READY = "IR_READY"
DOC_PROCESSING = "DOC_PROCESSING"
METADATA_READY = "METADATA_READY"
GENERATING_MCP = "GENERATING_MCP"
VALIDATING = "VALIDATING"
DEPLOYING = "DEPLOYING"
DEPLOYED = "DEPLOYED"
UNDER_REVIEW = "UNDER_REVIEW"
APPROVED = "APPROVED"
PUBLISHED = "PUBLISHED"
ACTIVE = "ACTIVE"

# Failure states (LLD §4.1). They branch off the main line and are resumable:
# a tool that failed translation goes back to TRANSLATING, not to DRAFT.
TRANSLATION_FAILED = "TRANSLATION_FAILED"
DOCUMENTATION_FAILED = "DOCUMENTATION_FAILED"
GENERATION_FAILED = "GENERATION_FAILED"
DEPLOYMENT_FAILED = "DEPLOYMENT_FAILED"
REJECTED = "REJECTED"
SUSPENDED = "SUSPENDED"

# Retirement. Not in the LLD's diagram, but its event list names
# `tool.deprecated` and `tool.archived`, and an event that announces a state no
# state machine can reach would be an event nobody could act on.
DEPRECATED = "DEPRECATED"
ARCHIVED = "ARCHIVED"

# Who may see a tool. Distinct from lifecycle: a PUBLISHED private tool is
# finished and visible to its own tenant, which is a normal thing to want.
VISIBILITY_PRIVATE = "private"
VISIBILITY_ORGANIZATION = "organization"
VISIBILITY_PUBLIC = "public"
VISIBILITIES = (VISIBILITY_PRIVATE, VISIBILITY_ORGANIZATION, VISIBILITY_PUBLIC)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegistryTool(SQLModel, table=True):
    """The authoritative record for one tool (ESDS LLD §3.7).

    *"Registry = authoritative system of record (metadata, versions, runtime
    refs, governance state). Marketplace = read-optimized storefront derived
    from Registry events."* Those two sentences are the design: nothing here is
    derived from anywhere, and nothing that reads this table writes back to it.

    `integration_id` is the catalog id the tool is served as once published,
    which is how a registry record and a marketplace listing describe the same
    thing. It is optional because a tool exists in the registry from the moment
    it is registered, long before anything can call it.

    `regions` and `compliance` are **provider declarations**, not verified
    facts. The distinction is kept in the field names' documentation and in the
    API response rather than being quietly lost — a tool claiming SOC 2 has
    claimed it, and nothing in this platform has checked.
    """

    __tablename__ = "registry_tool"
    __table_args__ = (UniqueConstraint("org_id", "tool_key", name="uq_registry_tool_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    # Stable, human-readable identity within a tenant. Never reused, never
    # renamed: it is what a subscription and a marketplace listing point at.
    tool_key: str = Field(index=True)
    name: str
    summary: str = ""
    description: str = ""

    category: str = "other"
    tags_json: str = Field(default="[]")
    # Declared by the provider, not verified by the platform.
    regions_json: str = Field(default="[]")
    compliance_json: str = Field(default="[]")

    visibility: str = Field(default=VISIBILITY_PRIVATE, index=True)
    lifecycle_state: str = Field(default=DRAFT, index=True)
    # Why the tool is in a failure state, when it is in one.
    state_reason: str = ""

    # Where it came from and what it is served as.
    project_id: uuid.UUID | None = Field(default=None, foreign_key="openapi_project.id")
    integration_id: str | None = Field(default=None, index=True)

    # Version pointers. `current_version` is the newest version created;
    # `published_version` is the one the marketplace shows, which can lag
    # behind while a newer version is still under review.
    current_version: int = Field(default=0)
    published_version: int | None = None

    deprecation_note: str = ""
    deprecated_at: datetime | None = None
    archived_at: datetime | None = None
    published_at: datetime | None = None

    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
