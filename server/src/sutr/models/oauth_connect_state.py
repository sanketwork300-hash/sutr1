import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OAuthConnectState(SQLModel, table=True):
    """A connected-account authorization in flight.

    One row per "Connect" click. The row carries who started the flow, because
    the callback arrives as a bare browser redirect with no sutr session
    attached — the state parameter is the only thing tying it back to a user,
    which is exactly why it must be single-use and short-lived.

    Distinct from ``google_login_state`` (signing *in* to sutr) and from
    ``oauth_state`` (an integration's upstream OAuth): those grant different
    things and must not share a namespace.
    """

    __tablename__ = "oauth_connect_state"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    state: str = Field(unique=True, index=True)
    provider: str = Field(index=True)
    org_id: uuid.UUID = Field(foreign_key="org.id")
    user_id: uuid.UUID = Field(foreign_key="user.id")
    code_verifier: str = ""
    # Device-grant bookkeeping (AWS only): the throwaway client registration
    # and device code the poller needs between calls. Plaintext because it is
    # a public-client registration whose only power is to finish this one
    # flow, and because the row is deleted the moment the flow ends.
    device_payload_json: str = Field(default="{}")
    # Where to send the browser once the connection is stored, so the user
    # lands back on the builder stage they left. Validated as a same-origin
    # relative path before use.
    redirect_after: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
