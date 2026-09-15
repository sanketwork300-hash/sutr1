import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# How long a key is honoured. Long enough that a client retrying after a
# network partition still gets its original answer; short enough that the table
# does not grow without bound.
RETENTION_HOURS = 24

STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"


class IdempotencyKey(SQLModel, table=True):
    """One remembered result for one `Idempotency-Key`.

    Build prompt §76 requires idempotency for billing, payments, registration,
    uploads, settlement, token issuance, tool publication and runtime
    deployment: *"Repeated request with same key must return the original
    result."*

    Two properties make that true rather than approximately true:

    - The key is scoped to `(org, endpoint, key)`. A key is a client's own
      token; it must not collide across tenants, and reusing it on a different
      endpoint is a mistake worth reporting rather than silently honouring.
    - `request_hash` pins what the key was used for. The same key with a
      *different* body is a client bug — replaying the first response would
      silently discard the second request — so it is a 409, not a replay.
    """

    __tablename__ = "idempotency_key"
    __table_args__ = (
        UniqueConstraint("org_id", "endpoint", "key", name="uq_idempotency_key_scope"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    # The route template, not the raw path: ids in the path are already part of
    # the request hash, and a template keeps the scope readable.
    endpoint: str = Field(index=True)
    key: str = Field(index=True)
    request_hash: str
    state: str = Field(default=STATE_IN_PROGRESS)
    status_code: int | None = None
    # The response body as sent, replayed verbatim on a repeat.
    response_json: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
