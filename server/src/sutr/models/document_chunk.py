import uuid
from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

# What kind of content a chunk holds. Following Unstructured's partition model:
# a document is a sequence of typed elements, and chunking groups them by
# section rather than by a fixed window.
ELEMENT_TITLE = "title"
ELEMENT_NARRATIVE = "narrative"
ELEMENT_LIST = "list"
ELEMENT_TABLE = "table"
ELEMENT_CODE = "code"


class DocumentChunk(SQLModel, table=True):
    """One semantically whole piece of a document.

    Fixed-size windows cut rules in half — "Refunds are allowed only within" is
    not a rule — so chunking follows the document's own structure: partition
    into typed elements, then group by heading. A chunk is what an extracted
    fact cites, and what retrieval returns.
    """

    __tablename__ = "document_chunk"
    __table_args__ = (Index("ix_document_chunk_doc_order", "document_id", "position"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    document_id: uuid.UUID = Field(foreign_key="document.id", index=True)

    position: int = Field(default=0)
    # The heading path this chunk sits under, e.g. "Refunds > Eligibility".
    # Retrieval shows it, and a citation reads far better with it.
    section_path: str = ""
    element_type: str = ELEMENT_NARRATIVE
    text: str = ""
    # Character offsets into the extracted plain text, so a citation can point
    # at a location rather than only at a chunk.
    start_offset: int = Field(default=0)
    end_offset: int = Field(default=0)
    # 1-based page, where the format has pages.
    page: int | None = None
    token_estimate: int = Field(default=0)

    created_at: datetime = Field(default_factory=datetime.utcnow)
