import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel


class Secret(SQLModel, table=True):
    __tablename__ = "secret"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID | None = Field(default=None, foreign_key="org.id", index=True)
    kind: str = Field(index=True)
    storage_backend: str = Field(index=True)
    value: str | None = None
    encrypted_data_key: str | None = None
    kms_key_id: str | None = None
    # Historically this row also stored an unsalted SHA-256 of the secret and its
    # first 12 plaintext characters. Nothing ever read them, and both leaked
    # material about the secret even under KMS (a short prefix often identifies
    # an API key outright, and an unsalted digest is offline-checkable), so
    # migration 0028 dropped them. Do not reintroduce a value-derived column.
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
