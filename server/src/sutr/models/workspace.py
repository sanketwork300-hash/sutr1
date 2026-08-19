import uuid
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Workspace(SQLModel, table=True):
    """A sub-division of an org for grouping integrations, APIs, and deployments.

    Every org gets a non-deletable default workspace. Existing org-scoped
    resources stay org-scoped for backward compatibility; new Sutr resources
    (OpenAPI projects, deployments) attach to a workspace.
    """

    __tablename__ = "workspace"
    __table_args__ = (UniqueConstraint("org_id", "slug", name="uq_workspace_org_slug"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    name: str
    slug: str
    is_default: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
