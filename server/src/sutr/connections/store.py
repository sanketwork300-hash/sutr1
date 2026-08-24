"""Persistence for connected accounts, and the one function that hands out a
usable access token.

Everything that needs to call a provider goes through `access_token()`, which
refreshes on the way out when the stored token is close to expiry. Callers
never see a refresh token and never decide whether one is due.

Tokens live in `secret` rows through the configured secrets backend; the
`provider_connection` row holds only pointers, an expiry, and a display label.
"""

import json
import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from sutr.connections.errors import ConnectError
from sutr.connections.flow import EXPIRY_SKEW, expires_at, refresh_access_token, utcnow
from sutr.connections.providers import get_provider
from sutr.models.provider_connection import ProviderConnection
from sutr.secrets.records import delete_secret, get_secret_value, upsert_secret


def _secret_ref(org_id: uuid.UUID, connection_id: uuid.UUID, slot: str) -> str:
    return f"connections/{org_id}/{connection_id}/{slot}"


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; comparisons must not explode."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def find_connection(
    session: Session, *, org_id: uuid.UUID, user_id: uuid.UUID, provider: str
) -> ProviderConnection | None:
    return session.exec(
        select(ProviderConnection)
        .where(ProviderConnection.org_id == org_id)
        .where(ProviderConnection.user_id == user_id)
        .where(ProviderConnection.provider == provider)
    ).first()


def list_connections(
    session: Session, *, org_id: uuid.UUID, user_id: uuid.UUID
) -> list[ProviderConnection]:
    return list(
        session.exec(
            select(ProviderConnection)
            .where(ProviderConnection.org_id == org_id)
            .where(ProviderConnection.user_id == user_id)
        ).all()
    )


def load_connection(
    session: Session, connection_id: uuid.UUID, *, org_id: uuid.UUID
) -> ProviderConnection | None:
    connection = session.get(ProviderConnection, connection_id)
    if connection is None or connection.org_id != org_id:
        return None
    return connection


def save_connection(
    session: Session,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    access_token_value: str,
    refresh_token_value: str | None,
    scopes: str,
    account_label: str,
    expires: datetime | None,
    metadata: dict | None = None,
) -> ProviderConnection:
    """Create or replace the (org, user, provider) connection.

    Re-authorizing overwrites in place so the row identity is stable and any
    deployment pointing at it keeps working. A provider that omits a refresh
    token on re-consent (Google does, unless prompted) must not blank the one
    already stored, or the connection silently becomes single-use.
    """
    connection = find_connection(session, org_id=org_id, user_id=user_id, provider=provider)
    if connection is None:
        connection = ProviderConnection(org_id=org_id, user_id=user_id, provider=provider)
        session.add(connection)
        session.flush()

    access = upsert_secret(
        session,
        org_id=org_id,
        kind=f"connection_{provider}_access",
        ref=_secret_ref(org_id, connection.id, "access"),
        value=access_token_value,
        secret_id=connection.access_secret_id,
    )
    connection.access_secret_id = access.id

    if refresh_token_value:
        refresh = upsert_secret(
            session,
            org_id=org_id,
            kind=f"connection_{provider}_refresh",
            ref=_secret_ref(org_id, connection.id, "refresh"),
            value=refresh_token_value,
            secret_id=connection.refresh_secret_id,
        )
        connection.refresh_secret_id = refresh.id

    connection.scopes = scopes
    connection.account_label = account_label
    connection.expires_at = expires
    if metadata is not None:
        connection.metadata_json = json.dumps(metadata)
    connection.updated_at = datetime.utcnow()
    session.add(connection)
    session.flush()
    return connection


def delete_connection(session: Session, connection: ProviderConnection) -> None:
    delete_secret(session, connection.access_secret_id)
    delete_secret(session, connection.refresh_secret_id)
    session.delete(connection)


def metadata_of(connection: ProviderConnection) -> dict:
    try:
        data = json.loads(connection.metadata_json)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def is_expired(connection: ProviderConnection) -> bool:
    expiry = _aware(connection.expires_at)
    return expiry is not None and expiry <= utcnow() + EXPIRY_SKEW


async def access_token(session: Session, connection: ProviderConnection) -> str:
    """A token that is valid right now, refreshing first if it has to be.

    Raises rather than returning a stale token: a caller that gets a string
    back is entitled to assume it works.
    """
    provider = get_provider(connection.provider)
    if provider is None:
        raise ConnectError("unknown_provider", f"Unknown provider '{connection.provider}'.")

    token = get_secret_value(session, connection.access_secret_id)
    if not is_expired(connection):
        if not token:
            raise ConnectError(
                "connection_broken",
                f"The stored {provider.display_name} token is missing. Reconnect the account.",
            )
        return token

    refresh_token = get_secret_value(session, connection.refresh_secret_id)
    if not refresh_token:
        # AWS device-grant sessions are the common case here: they expire and
        # there is nothing to refresh with, so say so instead of retrying.
        raise ConnectError(
            "reauthorization_required",
            f"The {provider.display_name} authorization has expired and cannot be "
            "renewed automatically. Reconnect the account.",
        )
    if provider.flow != "authorization_code":
        raise ConnectError(
            "reauthorization_required",
            f"The {provider.display_name} authorization has expired. Reconnect the account.",
        )

    payload = await refresh_access_token(provider, refresh_token)
    save_connection(
        session,
        org_id=connection.org_id,
        user_id=connection.user_id,
        provider=connection.provider,
        access_token_value=payload["access_token"],
        # Rotating providers send a new refresh token; static ones send none,
        # and save_connection keeps the existing one in that case.
        refresh_token_value=payload.get("refresh_token"),
        scopes=payload.get("scope") or connection.scopes,
        account_label=connection.account_label,
        expires=expires_at(payload),
        metadata=None,
    )
    session.commit()
    session.refresh(connection)
    return payload["access_token"]


def serialize(connection: ProviderConnection) -> dict:
    provider = get_provider(connection.provider)
    return {
        "id": str(connection.id),
        "provider": connection.provider,
        "display_name": provider.display_name if provider else connection.provider,
        "kind": provider.kind if provider else "source",
        "account_label": connection.account_label,
        "scopes": connection.scopes,
        "expires_at": _aware(connection.expires_at).isoformat() if connection.expires_at else None,
        "expired": is_expired(connection),
        "metadata": metadata_of(connection),
        "created_at": connection.created_at.isoformat(),
        "updated_at": connection.updated_at.isoformat(),
    }
