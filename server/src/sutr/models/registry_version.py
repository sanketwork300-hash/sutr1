import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegistryVersion(SQLModel, table=True):
    """One immutable version of a registry tool (ESDS LLD §3.7).

    *"every version immutable (v1→v2→v3…) with build hash, SBOM, deployment
    manifest · old versions kept for rollback"*. All four clauses are load-
    bearing and all four are here.

    Nothing updates this row after it is written, and nothing deletes it — not
    even archiving the tool. A version that could be edited would make a
    rollback a promise about bytes that may have changed since, and a version
    that could be deleted would make rollback a promise that can be broken by
    somebody tidying up.

    The build hash, SBOM and manifest are **copied** from the runtime artifact
    rather than referenced through it. `artifact_id` is kept as provenance, but
    a version must stay readable and rollback-able even if the artifact row is
    gone, and a foreign key that a rollback depends on is a rollback with a
    dependency nobody remembers.
    """

    __tablename__ = "registry_version"
    __table_args__ = (UniqueConstraint("tool_id", "version", name="uq_registry_version_number"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    tool_id: uuid.UUID = Field(foreign_key="registry_tool.id", index=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    # Monotonic within a tool, starting at 1.
    version: int

    # Provenance, and the three things the LLD requires a version to carry.
    artifact_id: uuid.UUID | None = Field(default=None, foreign_key="runtime_artifact.id")
    build_hash: str = ""
    sbom_json: str = Field(default="{}")
    deployment_manifest_json: str = Field(default="{}")

    tool_count: int = 0
    notes: str = ""
    # Whether the version was ever the published one. History, not a flag that
    # anything branches on: it is what makes "which version were they using in
    # March" answerable.
    was_published: bool = Field(default=False)

    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
