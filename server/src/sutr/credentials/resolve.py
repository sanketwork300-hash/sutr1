"""Turn stored credentials into the values one outbound request carries.

This is the only place a credential value is read back out of the secrets
backend for a compiled API, and the values never leave the request: they are
handed to `runtime.request_builder.apply_credentials` and discarded.

The OAuth2 client-credentials grant is executed here rather than delegated to
the user. Build prompt §24 forbids degrading OAuth2 into a pasted bearer
token, and Sutr already owns every piece of the machinery — a token endpoint
call, a cached token, and a refresh when it expires.
"""

import logging
import uuid
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session

from sutr.credentials.read import list_credentials
from sutr.models.integration_credential import (
    KIND_AUTHORIZATION_CODE,
    KIND_BASIC,
    KIND_CLIENT_CREDENTIALS,
    KIND_SECRET,
    IntegrationCredential,
)
from sutr.secrets.records import get_secret_value, upsert_secret
from sutr.upstream_safety import UnsafeUpstreamUrlError, validate_safe_url

logger = logging.getLogger(__name__)

# Refresh slightly before expiry so a token cannot lapse mid-request.
_EXPIRY_SKEW = timedelta(seconds=60)
_TOKEN_REQUEST_TIMEOUT = 15.0


async def resolve_for_integration(
    session: Session, org_id: uuid.UUID, integration_id: str
) -> list[dict]:
    """Every credential this integration should send, ready for the request.

    A credential that is not configured, or whose grant fails, is omitted with
    a log line rather than raising: the upstream API's own 401 is a better
    error for the caller than an internal exception, and one broken scheme
    must not stop the others from being sent.
    """
    resolved: list[dict] = []
    for credential in list_credentials(session, org_id, integration_id):
        value = await _value_for(session, credential)
        if not value:
            continue
        resolved.append(
            {
                "location": credential.location,
                "name": credential.param_name,
                "format": credential.value_format,
                "value": value,
            }
        )
    return resolved


async def _value_for(session: Session, credential: IntegrationCredential) -> str | None:
    if credential.kind in (KIND_SECRET, KIND_BASIC):
        return get_secret_value(session, credential.secret_id)
    if credential.kind == KIND_CLIENT_CREDENTIALS:
        return await client_credentials_token(session, credential)
    if credential.kind == KIND_AUTHORIZATION_CODE:
        # The grant is held by the connected-accounts subsystem; a compiled
        # integration reaches it through the installed integration's OAuth
        # state, which the dispatch path already resolves. Nothing to add here.
        return None
    return None


def _is_expired(credential: IntegrationCredential) -> bool:
    if credential.expires_at is None:
        return True
    return datetime.utcnow() >= credential.expires_at - _EXPIRY_SKEW


async def client_credentials_token(
    session: Session, credential: IntegrationCredential
) -> str | None:
    """The current access token for a client-credentials grant, obtaining one
    if there is none or it is about to expire."""
    if not _is_expired(credential):
        cached = get_secret_value(session, credential.access_token_secret_id)
        if cached:
            return cached

    if not (credential.client_id and credential.client_secret_id and credential.token_url):
        return None

    client_secret = get_secret_value(session, credential.client_secret_id)
    if not client_secret:
        return None

    try:
        # The token URL comes from a user-supplied specification, so it is
        # screened like any other upstream target.
        validate_safe_url(credential.token_url)
    except UnsafeUpstreamUrlError as exc:
        # nosemgrep: python-logger-credential-disclosure
        logger.warning(
            "refusing OAuth2 token request for %s/%s: %s",
            credential.integration_id,
            credential.scheme_name,
            exc,
        )
        return None

    data = {"grant_type": "client_credentials"}
    if credential.scopes:
        data["scope"] = credential.scopes

    try:
        async with httpx.AsyncClient(
            timeout=_TOKEN_REQUEST_TIMEOUT, follow_redirects=False
        ) as client:
            response = await client.post(
                credential.token_url,
                data=data,
                auth=(credential.client_id, client_secret),
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        # nosemgrep: python-logger-credential-disclosure
        logger.warning(
            "OAuth2 token request failed for %s/%s: %s",
            credential.integration_id,
            credential.scheme_name,
            exc,
        )
        return None

    if response.status_code >= 400:
        # nosemgrep: python-logger-credential-disclosure
        logger.warning(
            "OAuth2 token endpoint returned %s for %s/%s",
            response.status_code,
            credential.integration_id,
            credential.scheme_name,
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        # nosemgrep: python-logger-credential-disclosure
        logger.warning(
            "OAuth2 token endpoint returned a non-JSON body for %s/%s",
            credential.integration_id,
            credential.scheme_name,
        )
        return None

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        # nosemgrep: python-logger-credential-disclosure
        logger.warning(
            "OAuth2 token response carried no access_token for %s/%s",
            credential.integration_id,
            credential.scheme_name,
        )
        return None

    expires_in = payload.get("expires_in")
    try:
        lifetime = int(expires_in)
    except (TypeError, ValueError):
        # Unspecified lifetimes are common; a short cache is safer than
        # assuming the token lasts forever.
        lifetime = 300

    secret = upsert_secret(
        session,
        org_id=credential.org_id,
        kind=f"integration_access_token:{credential.integration_id}:{credential.scheme_name}",
        ref=f"{credential.integration_id}:{credential.scheme_name}:access_token",
        value=token,
        secret_id=credential.access_token_secret_id,
    )
    credential.access_token_secret_id = secret.id
    credential.expires_at = datetime.utcnow() + timedelta(seconds=max(lifetime, 30))
    credential.updated_at = datetime.utcnow()
    session.add(credential)
    session.commit()
    return token
