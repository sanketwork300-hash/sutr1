import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderProfile(SQLModel, table=True):
    """How a provider presents itself in the marketplace (ESDS LLD §3.7).

    One per organization. Everything on it is the provider's own copy — except
    `verified`, which is deliberately **not** settable by the provider: a
    verification badge a provider can award itself is a badge that means
    nothing. It is set by a platform administrator, and `verified_note` records
    on what basis.
    """

    __tablename__ = "provider_profile"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True, unique=True)

    display_name: str = ""
    summary: str = ""
    website_url: str = ""
    support_email: str = ""
    support_url: str = ""

    verified: bool = Field(default=False)
    verified_note: str = ""
    verified_at: datetime | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
