import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrgInvitation(SQLModel, table=True):
    """An email invitation to join an org with a given role.

    The raw invite token is shown/emailed once; only its SHA-256 lands here.
    Lifecycle: pending (accepted_at/revoked_at both null, expires_at in the
    future) → accepted | revoked | expired (lazy, judged at read time).
    """

    __tablename__ = "org_invitation"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    email: str = Field(index=True)  # normalized lowercase
    role: str = Field(default="member")
    token_hash: str = Field(unique=True, index=True)
    invited_by_user_id: uuid.UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    accepted_at: datetime | None = Field(default=None)
    accepted_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    revoked_at: datetime | None = Field(default=None)
