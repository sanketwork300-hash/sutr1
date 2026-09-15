import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel, UniqueConstraint

EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"
EFFECTS = (EFFECT_ALLOW, EFFECT_DENY)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccessRule(SQLModel, table=True):
    """One attribute-based authorization rule (ESDS LLD §4.3.3).

    The LLD's own example is the shape this has to express:

        Finance role ∧ Organization=Bank-A ∧ Region=India ⇒ Allow Refund Tool

    A rule matches on **subject** attributes (role, department, region, and
    whatever else the tenant puts on an agent identity), on **resource**
    attributes (integration, tool, category), and on an action. An unspecified
    matcher matches anything — a rule that named every attribute would have to
    be rewritten every time one was added.

    Two decisions make the evaluation predictable:

    - **Deny wins, always.** A tenant that has written a deny rule has decided
      something; an allow rule elsewhere must not quietly overrule it. Priority
      orders the *reporting*, never the outcome.
    - **No rules means no opinion**, not "deny everything". A tenant that has
      not written any ABAC rules should keep the behaviour they had before the
      layer existed, and a layer that starts denying the moment it is deployed
      is a layer nobody will deploy.
    """

    __tablename__ = "access_rule"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_access_rule_name"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    name: str = Field(index=True)
    description: str = ""

    effect: str = Field(default=EFFECT_ALLOW, index=True)
    # Lower runs first. Only affects which rule is *reported* as the reason;
    # a deny anywhere in the set still wins.
    priority: int = Field(default=100)

    # {"role": "finance", "region": "india", ...} — every entry must match.
    subject_json: str = Field(default="{}")
    # {"integration_id": "refunds", "tool_name": "refund_payment", ...}
    resource_json: str = Field(default="{}")
    # "invoke" today; named so a second action does not need a migration.
    action: str = Field(default="invoke", index=True)

    enabled: bool = Field(default=True, index=True)
    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
