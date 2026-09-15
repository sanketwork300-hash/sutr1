import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel


class DocWorkflow(SQLModel, table=True):
    """A process described in prose, turned into a graph.

    The LLD's example (§3.5): *Refund Request → Verify Payment → Eligibility
    Check → Approval → Refund → Notification*.

    Nodes and edges are stored as JSON rather than as two more tables: a
    workflow is read and written whole, never queried step by step, and two
    tables would buy joins nobody performs.

    `operation_refs` and `tool_refs` are the link back to the API — build
    prompt §17 requires a workflow to carry its associated operations and
    tools, which is what makes it useful to an agent rather than to a reader.
    """

    __tablename__ = "doc_workflow"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    document_id: uuid.UUID = Field(foreign_key="document.id", index=True)
    chunk_id: uuid.UUID | None = Field(default=None, foreign_key="document_chunk.id")
    job_id: uuid.UUID | None = Field(default=None, foreign_key="document_job.id")

    name: str = ""
    description: str = ""
    # [{"id": "...", "label": "...", "order": 0, "condition": "..."}]
    nodes_json: str = Field(default="[]")
    # [{"from": "...", "to": "...", "condition": "..."}]
    edges_json: str = Field(default="[]")

    # operationIds this workflow's steps appear to correspond to, and the tools
    # generated from them. Matched, never invented — an unmatched step stays
    # unmatched rather than being attached to the nearest-looking operation.
    operation_refs_json: str = Field(default="[]")
    tool_refs_json: str = Field(default="[]")

    source_document: str = ""
    source_location: str = ""
    source_text: str = ""
    confidence: float = Field(default=0.0)
    extractor_version: str = ""

    created_at: datetime = Field(default_factory=datetime.utcnow)


class GlossaryTerm(SQLModel, table=True):
    """A term the provider's documentation defines.

    The LLD lists these alongside rules and workflows (§3.5): *Chargeback,
    Settlement, Refund, Authorization Hold*. An agent that does not know what
    "authorization hold" means in this provider's vocabulary will pick the
    wrong tool.
    """

    __tablename__ = "glossary_term"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    document_id: uuid.UUID = Field(foreign_key="document.id", index=True)
    chunk_id: uuid.UUID | None = Field(default=None, foreign_key="document_chunk.id")
    job_id: uuid.UUID | None = Field(default=None, foreign_key="document_job.id")

    term: str = Field(default="", index=True)
    definition: str = ""
    # Other spellings and abbreviations seen for the same term.
    aliases_json: str = Field(default="[]")

    source_document: str = ""
    source_location: str = ""
    source_text: str = ""
    confidence: float = Field(default=0.0)
    extractor_version: str = ""

    created_at: datetime = Field(default_factory=datetime.utcnow)
