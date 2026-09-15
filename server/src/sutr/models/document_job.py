import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel

# The pipeline, in order (ESDS LLD §3.5).
STAGE_PARSE = "parse"
STAGE_CHUNK = "chunk"
STAGE_EXTRACT = "extract"
STAGE_GRAPH = "graph"
STAGE_EMBED = "embed"

STAGES = (STAGE_PARSE, STAGE_CHUNK, STAGE_EXTRACT, STAGE_GRAPH, STAGE_EMBED)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
# Finished, having skipped something that was allowed to be skipped.
STATUS_PARTIAL = "partial"


class DocumentJob(SQLModel, table=True):
    """One run of the documentation pipeline over one document.

    LLD §3.5 requires a *checkpoint per stage*, so a failure resumes rather
    than restarting: `completed_stages` is that checkpoint, and re-running a
    job skips what already succeeded.

    The distinction between `failed` and `partial` is the LLD's own: OCR that
    cannot read a page flags the page and continues; a graph backend that is
    absent is skipped with a warning. Neither is a failed job, and calling them
    one would make people ignore real failures.
    """

    __tablename__ = "document_job"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    document_id: uuid.UUID = Field(foreign_key="document.id", index=True)

    status: str = Field(default=STATUS_QUEUED, index=True)
    current_stage: str | None = None
    # Stages that finished, in order. The resume point.
    completed_stages_json: str = Field(default="[]")
    # Per-stage outcome, timing and counts — the inspectable record.
    stage_results_json: str = Field(default="[]")

    error_code: str | None = None
    error_message: str | None = None
    # Non-fatal losses, each naming what was skipped and why.
    degradations_json: str = Field(default="[]")

    # Which extractor produced the facts in this run. Build prompt §16 requires
    # a `model_version` on every rule; this is where the run's identity lives.
    extractor_version: str = ""

    attempts: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
