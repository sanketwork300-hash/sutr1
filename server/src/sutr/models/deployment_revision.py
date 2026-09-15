import uuid
from datetime import datetime

from sqlmodel import Field, LargeBinary, SQLModel

# What produced this revision.
ORIGIN_CREATE = "create"
ORIGIN_UPDATE = "update"
ORIGIN_ROLLBACK = "rollback"

OUTCOME_PENDING = "pending"
OUTCOME_ACTIVE = "active"
OUTCOME_SUPERSEDED = "superseded"
OUTCOME_FAILED = "failed"


class DeploymentRevision(SQLModel, table=True):
    """One immutable version of a deployment.

    Build prompt §36 forbids implementing an update as delete-then-recreate and
    requires versioned artifacts with history and true rollback. This row is
    that history: it keeps the package that was deployed, the configuration it
    was deployed with, and the provider state it produced.

    Keeping the package (rather than a pointer to the source project) is the
    point — a rollback re-runs an artifact that was actually built and actually
    ran, not a rebuild from source that has since changed.
    """

    __tablename__ = "deployment_revision"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    deployment_id: uuid.UUID = Field(foreign_key="deployment.id", index=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    # Monotonic within a deployment, starting at 1.
    revision: int
    origin: str = ORIGIN_CREATE
    # When origin is `rollback`, which revision was restored.
    restored_from_revision: int | None = None
    outcome: str = OUTCOME_PENDING
    error: str | None = None
    # Which validated artifact produced this revision's package, when one did.
    # Kept per revision rather than only on the deployment so a rollback can
    # say which build it restored, not merely which bytes.
    artifact_id: uuid.UUID | None = Field(default=None, foreign_key="runtime_artifact.id")
    # The exact package that was deployed, and the placement it used.
    package_zip: bytes = Field(sa_type=LargeBinary)
    package_sha256: str = ""
    config_json: str = Field(default="{}")
    tool_count: int = Field(default=0)
    # What the provider returned. Opaque to the platform, by design.
    provider_state_json: str = Field(default="{}")
    url: str | None = None
    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
