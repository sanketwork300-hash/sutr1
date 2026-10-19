import hashlib
import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_key"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    created_by_user_id: uuid.UUID = Field(foreign_key="user.id")
    name: str
    key_prefix: str  # first 12 chars of plain key, for display only
    key_hash: str = Field(index=True)  # `hash_api_key` of the full plain key
    # Org role the key acts with. Captured from the creator's membership at
    # creation time so a key can never do more than the human who minted it;
    # `ensure_agent_can` checks it through the same matrix as a user session.
    role: str = Field(default="developer")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: datetime | None = Field(default=None)
    is_active: bool = Field(default=True)


def hash_api_key(plain_key: str) -> str:
    """The lookup digest stored for an API key: SHA-256, hex.

    An API key is not a password. It is 192 bits from `secrets.token_urlsafe`,
    so there is nothing to brute-force and a slow, salted KDF buys nothing; it
    would also make the digest unusable as the indexed lookup every request
    performs. A fast, unsalted hash is the right tool for a high-entropy
    token. Changing the function orphans every key already issued.
    """
    return hashlib.sha256(plain_key.encode()).hexdigest()
