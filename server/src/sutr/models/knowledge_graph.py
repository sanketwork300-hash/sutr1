import uuid
from datetime import datetime

from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

# Entity types the LLD names (§3.5, build prompt §18).
ENTITY_PROVIDER = "Provider"
ENTITY_CUSTOMER = "Customer"
ENTITY_ACCOUNT = "Account"
ENTITY_PAYMENT = "Payment"
ENTITY_REFUND = "Refund"
ENTITY_API = "API"
ENTITY_TOOL = "Tool"
ENTITY_WORKFLOW = "Workflow"
ENTITY_BUSINESS_RULE = "BusinessRule"
ENTITY_ROLE = "Role"
ENTITY_REGION = "Region"
ENTITY_COMPLIANCE = "ComplianceRequirement"
ENTITY_TERM = "Term"
ENTITY_DOCUMENT = "Document"

ENTITY_TYPES = (
    ENTITY_PROVIDER,
    ENTITY_CUSTOMER,
    ENTITY_ACCOUNT,
    ENTITY_PAYMENT,
    ENTITY_REFUND,
    ENTITY_API,
    ENTITY_TOOL,
    ENTITY_WORKFLOW,
    ENTITY_BUSINESS_RULE,
    ENTITY_ROLE,
    ENTITY_REGION,
    ENTITY_COMPLIANCE,
    ENTITY_TERM,
    ENTITY_DOCUMENT,
)

# Relationship types (build prompt §18).
REL_OWNS = "OWNS"
REL_CONTAINS = "CONTAINS"
REL_ELIGIBLE_FOR = "ELIGIBLE_FOR"
REL_IMPLEMENTS = "IMPLEMENTS"
REL_REQUIRES = "REQUIRES"
REL_MAY_INVOKE = "MAY_INVOKE"
REL_CONTAINS_OPERATION = "CONTAINS_OPERATION"
REL_DEFINED_IN = "DEFINED_IN"
REL_MENTIONS = "MENTIONS"
REL_NEXT = "NEXT"

RELATIONSHIP_TYPES = (
    REL_OWNS,
    REL_CONTAINS,
    REL_ELIGIBLE_FOR,
    REL_IMPLEMENTS,
    REL_REQUIRES,
    REL_MAY_INVOKE,
    REL_CONTAINS_OPERATION,
    REL_DEFINED_IN,
    REL_MENTIONS,
    REL_NEXT,
)


class KnowledgeNode(SQLModel, table=True):
    """One entity in the knowledge graph.

    Build prompt §18 prefers Neo4j, and the graph backend is pluggable
    (ADR-015) — but the default install has no Neo4j, and a graph that only
    exists when an optional service is running is a graph nobody can rely on.
    So the graph is stored here too, always, and Neo4j becomes a mirror for
    deployments that have it.

    The invariant the LLD states plainly: *"Graph is enrichment. It must never
    replace the canonical IR."* Nothing reads tool definitions from here.
    """

    __tablename__ = "knowledge_node"
    __table_args__ = (
        # One node per (org, type, key): re-processing a document must update
        # the graph rather than duplicate it.
        UniqueConstraint("org_id", "entity_type", "entity_key", name="uq_knowledge_node_identity"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    entity_type: str = Field(index=True)
    # Stable identity within its type: a normalized term, an operationId, a
    # workflow id. What makes the node the same node on the next run.
    entity_key: str = Field(index=True)
    label: str = ""
    properties_json: str = Field(default="{}")

    # Where this node came from, so the graph can be traced back like anything
    # else extracted from prose.
    document_id: uuid.UUID | None = Field(default=None, foreign_key="document.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class KnowledgeEdge(SQLModel, table=True):
    """One typed, directed relationship."""

    __tablename__ = "knowledge_edge"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "source_node_id",
            "relationship",
            "target_node_id",
            name="uq_knowledge_edge_identity",
        ),
        Index("ix_knowledge_edge_source", "org_id", "source_node_id"),
        Index("ix_knowledge_edge_target", "org_id", "target_node_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    source_node_id: uuid.UUID = Field(foreign_key="knowledge_node.id")
    target_node_id: uuid.UUID = Field(foreign_key="knowledge_node.id")
    relationship: str = Field(index=True)
    properties_json: str = Field(default="{}")
    document_id: uuid.UUID | None = Field(default=None, foreign_key="document.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
