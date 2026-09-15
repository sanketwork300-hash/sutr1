"""Per-scheme credentials for a compiled API integration.

A compiled integration can declare several security schemes, and they are not
interchangeable: an API key belongs in one specific header, query parameter,
or cookie, and an OAuth2 client-credentials grant needs a client id and secret
rather than a token. These endpoints let each declared scheme be configured on
its own terms (ADR-009).

No endpoint here ever returns a credential value. `GET` reports which schemes
the specification declared, what each one needs, and whether it is configured.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from sutr.api.custom_api import _invalidate_tool_cache
from sutr.authz import ensure_agent_can
from sutr.credentials.store import (
    delete_credential,
    describe,
    encode_basic,
    list_credentials,
    upsert_credential,
)
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.models.custom_api_integration import CustomApiIntegration
from sutr.models.integration_credential import KIND_BASIC, KIND_CLIENT_CREDENTIALS, KIND_SECRET
from sutr.openapi.security import UNSUPPORTED, AuthTranslation, CredentialPlacement
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/api/integrations/{integration_id}/credentials", tags=["integrations"])


class CredentialRequest(BaseModel):
    scheme_name: str
    # For apiKey / bearer schemes.
    value: str | None = None
    # For basic schemes — encoded server-side so the stored secret is exactly
    # what the header carries.
    username: str | None = None
    password: str | None = None
    # For OAuth2 client-credentials schemes.
    client_id: str | None = None
    client_secret: str | None = None


def _integration(session: Session, org_id: uuid.UUID, integration_id: str) -> CustomApiIntegration:
    integration = session.exec(
        select(CustomApiIntegration)
        .where(CustomApiIntegration.org_id == org_id)
        .where(CustomApiIntegration.integration_id == integration_id)
    ).first()
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found")
    return integration


def _placements(integration: CustomApiIntegration) -> list[CredentialPlacement]:
    if not integration.auth_json:
        return []
    return AuthTranslation.model_validate_json(integration.auth_json).placements


def _placement(integration: CustomApiIntegration, scheme_name: str) -> CredentialPlacement:
    for placement in _placements(integration):
        if placement.scheme_name == scheme_name:
            return placement
    raise HTTPException(
        status_code=404,
        detail=f"The API declares no security scheme named '{scheme_name}'.",
    )


@router.get("")
def list_integration_credentials(
    integration_id: str,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Which credentials this API declares, and which are configured."""
    ensure_agent_can(session, agent_auth, "integrations:manage")
    integration = _integration(session, agent_auth.org.id, integration_id)
    configured = {
        c.scheme_name: describe(c)
        for c in list_credentials(session, agent_auth.org.id, integration_id)
    }
    schemes = [
        {
            "scheme_name": placement.scheme_name,
            "scheme_type": placement.scheme_type,
            "location": placement.location,
            "param_name": placement.name,
            "requires": placement.requires,
            "usable": placement.usable,
            "hint": placement.credential_hint,
            "flow": placement.flow.model_dump() if placement.flow else None,
            "credential": configured.get(placement.scheme_name),
        }
        for placement in _placements(integration)
    ]
    return {
        "integration_id": integration_id,
        # The single-header credential the integration was installed with;
        # still used when no per-scheme credential is configured.
        "primary_token_header": integration.token_header,
        "schemes": schemes,
    }


@router.put("", status_code=200)
def set_integration_credential(
    integration_id: str,
    body: CredentialRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Configure the credential for one declared scheme."""
    ensure_agent_can(session, agent_auth, "integrations:manage")
    integration = _integration(session, agent_auth.org.id, integration_id)
    placement = _placement(integration, body.scheme_name)

    if placement.requires == UNSUPPORTED:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_scheme",
                "message": placement.credential_hint,
            },
        )

    kind_value = None
    client_id = None
    client_secret = None
    from sutr.credentials.store import kind_for

    kind = kind_for(placement)
    if kind == KIND_SECRET:
        if not body.value:
            raise HTTPException(status_code=400, detail="value is required for this scheme")
        kind_value = body.value
    elif kind == KIND_BASIC:
        if body.value:
            kind_value = body.value
        elif body.username is not None and body.password is not None:
            kind_value = encode_basic(body.username, body.password)
        else:
            raise HTTPException(
                status_code=400,
                detail="username and password (or a pre-encoded value) are required",
            )
    elif kind == KIND_CLIENT_CREDENTIALS:
        if not (body.client_id and body.client_secret):
            raise HTTPException(
                status_code=400,
                detail="client_id and client_secret are required for a client-credentials grant",
            )
        client_id, client_secret = body.client_id, body.client_secret
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "not_configurable_here",
                "message": (
                    "This scheme uses an authorization-code grant; authorize it through a "
                    "connected account instead."
                ),
            },
        )

    credential = upsert_credential(
        session,
        org_id=agent_auth.org.id,
        integration_id=integration_id,
        placement=placement,
        value=kind_value,
        client_id=client_id,
        client_secret=client_secret,
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="integration.credential.set",
        summary=(f"Credential for security scheme '{body.scheme_name}' on '{integration_id}' set"),
        target_type="integration",
        target_id=integration_id,
        metadata={"scheme_name": body.scheme_name, "kind": credential.kind},
        **actor_from_agent_auth(agent_auth),
    )
    _invalidate_tool_cache(session, agent_auth.org.id, integration_id)
    session.commit()
    return describe(credential)


@router.delete("/{scheme_name}", status_code=204)
def remove_integration_credential(
    integration_id: str,
    scheme_name: str,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> None:
    ensure_agent_can(session, agent_auth, "integrations:manage")
    _integration(session, agent_auth.org.id, integration_id)
    if not delete_credential(session, agent_auth.org.id, integration_id, scheme_name):
        raise HTTPException(status_code=404, detail="No credential configured for that scheme")
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="integration.credential.removed",
        summary=(f"Credential for security scheme '{scheme_name}' on '{integration_id}' removed"),
        target_type="integration",
        target_id=integration_id,
        metadata={"scheme_name": scheme_name},
        **actor_from_agent_auth(agent_auth),
    )
    _invalidate_tool_cache(session, agent_auth.org.id, integration_id)
    session.commit()
