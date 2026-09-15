import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# What kind of material this row holds. Mirrors `openapi/security.py`'s
# `requires` values, because the row is created from a placement.
KIND_SECRET = "secret"  # API key or bearer token
KIND_BASIC = "basic"  # username:password, stored already base64-encoded
KIND_CLIENT_CREDENTIALS = "client_credentials"  # OAuth2, run by the platform
KIND_AUTHORIZATION_CODE = "authorization_code"  # OAuth2 via a connected account

KINDS = (KIND_SECRET, KIND_BASIC, KIND_CLIENT_CREDENTIALS, KIND_AUTHORIZATION_CODE)


class IntegrationCredential(SQLModel, table=True):
    """One credential for one security scheme of one integration.

    Sutr used to hold exactly one credential per integration, sent as one
    header. Real specifications declare several schemes, place API keys in
    query strings and cookies, and expect OAuth2 grants to actually be run —
    so a credential is now a row per scheme (ADR-009).

    Nothing secret is stored on this row. `secret_id` and
    `client_secret_id` point into the `secret` table, which is where the
    configured secrets backend (plaintext or KMS envelope) applies.
    """

    __tablename__ = "integration_credential"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "integration_id",
            "scheme_name",
            name="uq_integration_credential_scheme",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    # The integration this credential belongs to, e.g. "customapi_stripe".
    integration_id: str = Field(index=True)
    # The security scheme name as the specification declared it.
    scheme_name: str
    kind: str = KIND_SECRET

    # Placement on the wire, copied from the translated scheme so the runtime
    # does not have to re-read the specification on every call.
    location: str = "header"  # header | query | cookie
    param_name: str = ""  # header name, query key, or cookie name
    value_format: str = "{token}"  # carries the literal {token} slot

    # Static credential (KIND_SECRET, KIND_BASIC).
    secret_id: uuid.UUID | None = Field(default=None, foreign_key="secret.id")

    # OAuth2 client credentials (KIND_CLIENT_CREDENTIALS).
    client_id: str | None = None
    client_secret_id: uuid.UUID | None = Field(default=None, foreign_key="secret.id")
    token_url: str | None = None
    scopes: str = ""  # space-separated, as the token endpoint expects

    # Cached access token for the grants the platform runs itself. Refreshed
    # in place; never returned to a client.
    access_token_secret_id: uuid.UUID | None = Field(default=None, foreign_key="secret.id")
    expires_at: datetime | None = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
