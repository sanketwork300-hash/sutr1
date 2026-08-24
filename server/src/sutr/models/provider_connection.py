import uuid
from datetime import datetime

from sqlmodel import Field, SQLModel


class ProviderConnection(SQLModel, table=True):
    """One OAuth-authorized link between a sutr user and an external provider.

    Providers fall into two groups and deliberately share this one table:
    spec sources (``github``) and deployment targets (``gcp``, ``azure``,
    ``aws``). Both answer the same question — "may sutr act on this account
    on your behalf?" — and both are revoked the same way.

    Tokens are never columns here: ``access_secret_id`` / ``refresh_secret_id``
    point at ``secret`` rows so the configured secrets backend (plain DB or
    KMS-wrapped) owns the ciphertext. Deleting the connection deletes both.

    Scoped to (org, user, provider): a connection is personal, because the
    grant is against the user's own GitHub/Google/Microsoft/AWS identity. Other
    members of the org see that a connection exists but cannot borrow it —
    ``account_label`` is shown, the token never is.
    """

    __tablename__ = "provider_connection"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", index=True)
    # github | gcp | azure | aws
    provider: str = Field(index=True)
    # Human-readable identity: GitHub login, Google/Microsoft email, AWS
    # Identity Center account name. Shown in the UI so a user can tell two
    # connections apart; never used for authorization.
    account_label: str = ""
    # Space-separated scopes actually granted (providers may narrow them).
    scopes: str = ""
    access_secret_id: uuid.UUID | None = Field(default=None, foreign_key="secret.id")
    refresh_secret_id: uuid.UUID | None = Field(default=None, foreign_key="secret.id")
    # None → the access token does not expire (GitHub OAuth app tokens).
    expires_at: datetime | None = None
    # Provider-specific detail the UI needs but that is not a credential:
    # AWS account/role ids, Azure tenant id, the SSO region, ...
    metadata_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
