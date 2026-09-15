import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

MIN_RATING = 1
MAX_RATING = 5


class MarketplaceReview(SQLModel, table=True):
    """One person's rating of one integration.

    Scoped to `(org, user, integration)`: a rating is an opinion held by a
    person, and one person holds one opinion per integration. Re-submitting
    updates the existing row rather than stacking a second vote, so the
    aggregate cannot be inflated by resubmission.

    The org is recorded because a self-hosted instance's ratings are its own —
    they are not shared with anyone, and nothing here is published anywhere.
    """

    __tablename__ = "marketplace_review"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "user_id", "integration_id", name="uq_marketplace_review_author"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    integration_id: str = Field(index=True)
    rating: int = Field(ge=MIN_RATING, le=MAX_RATING)
    title: str | None = Field(default=None, max_length=120)
    body: str | None = Field(default=None, max_length=4000)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
