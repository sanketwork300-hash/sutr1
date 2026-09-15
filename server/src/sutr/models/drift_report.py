import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel

# What produced the report.
ORIGIN_SYNC = "sync"  # a watched source changed
ORIGIN_MANUAL = "manual"  # somebody asked
ORIGIN_COMPARISON = "comparison"  # two sources disagree with each other

# What happened to it.
STATUS_OPEN = "open"  # reported, nobody has decided
STATUS_APPLIED = "applied"  # the change was taken into the project
STATUS_DISMISSED = "dismissed"  # a human looked and chose not to


class DriftReport(SQLModel, table=True):
    """A recorded difference between what the platform holds and what a source says.

    Kept rather than acted on immediately, because build prompt §13 is explicit:
    *"Breaking changes must not automatically deploy unless policy permits it."*
    A drift report is the thing a human looks at before that decision, and the
    record that the decision was made.
    """

    __tablename__ = "drift_report"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    project_id: uuid.UUID = Field(foreign_key="openapi_project.id", index=True)
    source_id: uuid.UUID = Field(foreign_key="api_source.id", index=True)
    # For a comparison report, the other source being compared against.
    compared_source_id: uuid.UUID | None = Field(default=None, foreign_key="api_source.id")

    origin: str = Field(default=ORIGIN_SYNC)
    status: str = Field(default=STATUS_OPEN, index=True)

    # Counts per category, so a list view needs no parsing.
    breaking_count: int = Field(default=0)
    non_breaking_count: int = Field(default=0)
    security_count: int = Field(default=0)
    documentation_count: int = Field(default=0)
    metadata_count: int = Field(default=0)

    # The full change list.
    changes_json: str = Field(default="[]")
    # Which IRs were compared. Enough to reproduce the comparison later.
    before_ir_hash: str | None = None
    after_ir_hash: str | None = None
    # The document that produced `after`, so an applied report needs no re-fetch
    # and a dismissed one can still be inspected.
    after_spec_text: str | None = None

    # Whether the sync policy allowed this to be applied automatically, and if
    # not, why. Recorded so "nothing happened" is explicable.
    auto_applied: bool = Field(default=False)
    withheld_reason: str | None = None

    detected_at: datetime = Field(default_factory=datetime.utcnow)
    resolved_at: datetime | None = None
    resolved_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
