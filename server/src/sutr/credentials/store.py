"""Writing per-scheme credentials.

The read side lives in `credentials/read.py` — the hot path resolves
credentials on every call and must not depend on the control plane (ADR-002).
The readers are re-exported here so existing callers are unaffected.
"""

import base64
import uuid
from datetime import datetime

from sqlmodel import Session

from sutr.credentials.read import get_credential, list_credentials
from sutr.models.integration_credential import (
    KIND_AUTHORIZATION_CODE,
    KIND_BASIC,
    KIND_CLIENT_CREDENTIALS,
    KIND_SECRET,
    IntegrationCredential,
)
from sutr.openapi.security import (
    NEEDS_AUTHORIZATION_CODE,
    NEEDS_BASIC,
    NEEDS_CLIENT_CREDENTIALS,
    NEEDS_SECRET,
    CredentialPlacement,
)
from sutr.secrets.records import delete_secret, upsert_secret

_REQUIRES_TO_KIND = {
    NEEDS_SECRET: KIND_SECRET,
    NEEDS_BASIC: KIND_BASIC,
    NEEDS_CLIENT_CREDENTIALS: KIND_CLIENT_CREDENTIALS,
    NEEDS_AUTHORIZATION_CODE: KIND_AUTHORIZATION_CODE,
}


def kind_for(placement: CredentialPlacement) -> str | None:
    return _REQUIRES_TO_KIND.get(placement.requires)


def encode_basic(username: str, password: str) -> str:
    """HTTP Basic's credential is the base64 of `username:password`.

    Encoded once here, on the way in, so the stored secret is exactly what the
    header carries and the runtime never has to know it was a pair.
    """
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def upsert_credential(
    session: Session,
    *,
    org_id: uuid.UUID,
    integration_id: str,
    placement: CredentialPlacement,
    value: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> IntegrationCredential:
    """Create or replace the credential for one scheme.

    `value` is the static credential for API-key, bearer, and (already
    base64-encoded) basic schemes. `client_id`/`client_secret` configure an
    OAuth2 client-credentials grant, whose access token the platform then
    obtains and refreshes itself.
    """
    kind = kind_for(placement)
    if kind is None:
        raise ValueError(
            f"Security scheme '{placement.scheme_name}' cannot hold a credential: "
            f"{placement.credential_hint}"
        )

    credential = get_credential(session, org_id, integration_id, placement.scheme_name)
    if credential is None:
        credential = IntegrationCredential(
            org_id=org_id,
            integration_id=integration_id,
            scheme_name=placement.scheme_name,
        )

    credential.kind = kind
    credential.location = placement.location
    credential.param_name = placement.name
    credential.value_format = placement.format
    credential.updated_at = datetime.utcnow()

    if kind in (KIND_SECRET, KIND_BASIC):
        if value:
            secret = upsert_secret(
                session,
                org_id=org_id,
                kind=f"integration_credential:{integration_id}:{placement.scheme_name}",
                ref=f"{integration_id}:{placement.scheme_name}",
                value=value,
                secret_id=credential.secret_id,
            )
            credential.secret_id = secret.id
    elif kind == KIND_CLIENT_CREDENTIALS:
        flow = placement.flow
        credential.token_url = flow.token_url if flow else None
        credential.scopes = " ".join(flow.scopes) if flow and flow.scopes else ""
        if client_id:
            credential.client_id = client_id
        if client_secret:
            secret = upsert_secret(
                session,
                org_id=org_id,
                kind=f"integration_client_secret:{integration_id}:{placement.scheme_name}",
                ref=f"{integration_id}:{placement.scheme_name}:client_secret",
                value=client_secret,
                secret_id=credential.client_secret_id,
            )
            credential.client_secret_id = secret.id
        # A changed client id or secret invalidates any cached access token.
        if client_id or client_secret:
            _forget_access_token(session, credential)

    session.add(credential)
    session.flush()
    return credential


def _forget_access_token(session: Session, credential: IntegrationCredential) -> None:
    delete_secret(session, credential.access_token_secret_id)
    credential.access_token_secret_id = None
    credential.expires_at = None


def delete_credential(
    session: Session, org_id: uuid.UUID, integration_id: str, scheme_name: str
) -> bool:
    credential = get_credential(session, org_id, integration_id, scheme_name)
    if credential is None:
        return False
    delete_secret(session, credential.secret_id)
    delete_secret(session, credential.client_secret_id)
    delete_secret(session, credential.access_token_secret_id)
    session.delete(credential)
    return True


def delete_all_for_integration(session: Session, org_id: uuid.UUID, integration_id: str) -> int:
    credentials = list_credentials(session, org_id, integration_id)
    for credential in credentials:
        delete_secret(session, credential.secret_id)
        delete_secret(session, credential.client_secret_id)
        delete_secret(session, credential.access_token_secret_id)
        session.delete(credential)
    return len(credentials)


def describe(credential: IntegrationCredential) -> dict:
    """A client-safe view. Never includes a secret value."""
    return {
        "scheme_name": credential.scheme_name,
        "kind": credential.kind,
        "location": credential.location,
        "param_name": credential.param_name,
        "configured": bool(
            credential.secret_id
            or (credential.client_id and credential.client_secret_id)
            or credential.access_token_secret_id
        ),
        "client_id": credential.client_id,
        "token_url": credential.token_url,
        "scopes": credential.scopes.split() if credential.scopes else [],
        "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
        "updated_at": credential.updated_at.isoformat(),
    }


__all__ = [
    "delete_all_for_integration",
    "delete_credential",
    "describe",
    "encode_basic",
    "get_credential",
    "kind_for",
    "list_credentials",
    "upsert_credential",
]
