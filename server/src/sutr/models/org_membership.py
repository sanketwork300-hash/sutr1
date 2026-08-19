import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel


class OrgMembership(SQLModel, table=True):
    __tablename__ = "org_membership"

    user_id: uuid.UUID = Field(foreign_key="user.id", primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", primary_key=True)
    # One of sutr.authz.ROLES: "owner" | "admin" | "developer" | "member" | "viewer".
    role: str = Field(default="owner")
    # Nullable because pre-0023 rows have no timestamp. Used (with org_id as a
    # tiebreak) to resolve a deterministic "primary" org for multi-org users.
    created_at: datetime | None = Field(default=None)
