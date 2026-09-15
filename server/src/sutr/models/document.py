import uuid
from datetime import datetime

from sqlmodel import Field, LargeBinary, SQLModel

# What a document is, as far as parsing is concerned.
KIND_PDF = "pdf"
KIND_HTML = "html"
KIND_MARKDOWN = "markdown"
KIND_DOCX = "docx"
KIND_TEXT = "text"

KINDS = (KIND_PDF, KIND_HTML, KIND_MARKDOWN, KIND_DOCX, KIND_TEXT)

STATUS_UPLOADED = "uploaded"
STATUS_PROCESSING = "processing"
STATUS_PROCESSED = "processed"
STATUS_FAILED = "failed"
# Processed, but something was lost on the way — pages that produced no text,
# a graph backend that was unavailable. The LLD is explicit that these continue
# rather than fail (§3.5), so they need a state that is neither.
STATUS_PARTIAL = "partial"


class Document(SQLModel, table=True):
    """A piece of provider documentation, kept as uploaded.

    ESDS LLD §3.5: documentation intelligence extracts *"what specs can't
    say"* — business rules, workflows, terminology, error guidance. This is the
    input to that, and the original is retained because every extracted fact
    has to be able to point back at the sentence it came from (build prompt
    §16: "Never lose the source citation").

    The LLD puts originals in object storage. There is no object store in the
    default install, so they live here — the same choice already made for
    deployment packages, and the same one to revisit when MinIO exists
    (ADR-015).
    """

    __tablename__ = "document"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    # The API this documentation describes, when it describes one. Optional:
    # a glossary or a policy document may not belong to a single API.
    project_id: uuid.UUID | None = Field(default=None, foreign_key="openapi_project.id", index=True)

    filename: str = ""
    media_type: str = ""
    kind: str = KIND_TEXT
    source_uri: str = ""
    size_bytes: int = 0
    sha256: str = Field(default="", index=True)
    content: bytes = Field(sa_type=LargeBinary)

    # What kind of document this is (documentation/classification.py), with the
    # evidence that produced the label. A label nobody can argue with is a
    # label nobody can correct.
    document_type: str = Field(default="unknown", index=True)
    classification_json: str = Field(default="{}")

    status: str = Field(default=STATUS_UPLOADED, index=True)
    # What was lost, if anything: unreadable pages, a skipped graph.
    degradations_json: str = Field(default="[]")

    uploaded_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: datetime | None = None
