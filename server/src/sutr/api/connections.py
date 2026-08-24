"""Connected accounts: authorize sutr against GitHub, Google Cloud, Azure, AWS.

One resource for both uses, because they are the same grant with different
consequences: GitHub authorizes *reading a specification*, the cloud providers
authorize *running a server*. Permissions differ accordingly - a source
connection needs `integrations:manage`, a deploy connection needs
`deployments:manage`.

The callback is the delicate part. It arrives as a bare browser redirect with
no sutr session on it, so the `state` row is the only thing binding the code
to a user. That row is single-use, TTL-bounded, and deleted before the code is
exchanged, whatever the outcome.
"""

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from sutr.authz import ensure_agent_can
from sutr.config import settings
from sutr.connections.device import poll_device_token, start_device_authorization
from sutr.connections.errors import ConnectError
from sutr.connections.flow import (
    authorization_url,
    exchange_code,
    expires_at,
    fetch_account_label,
    pkce_pair,
)
from sutr.connections.providers import (
    ALL_PROVIDERS,
    callback_url,
    get_provider,
    is_configured,
)
from sutr.connections.store import (
    access_token,
    delete_connection,
    find_connection,
    list_connections,
    load_connection,
    metadata_of,
    save_connection,
    serialize,
)
from sutr.connections.targets import aws_accounts, azure_subscriptions, gcp_projects
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.models.oauth_connect_state import OAuthConnectState
from sutr.openapi.errors import OpenAPIError
from sutr.openapi.sources import list_github_repositories
from sutr.services.audit import actor_from_agent_auth, record_audit, request_meta

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connections", tags=["connections"])

# An authorization the user never completes should not sit around; 15 minutes
# is longer than any consent screen takes and shorter than a coffee break.
STATE_TTL = timedelta(minutes=15)

# Which permission each provider's grant is worth.
_PERMISSION = {
    "github": "integrations:manage",
    "gcp": "deployments:manage",
    "azure": "deployments:manage",
    "aws": "deployments:manage",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _connect_error(exc: ConnectError) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": exc.code, "message": exc.message})


def _require_user(agent_auth: AgentAuth):
    """A connection is a personal grant, so an API key cannot open one.

    An API key belongs to the org, not to a person; letting it start an OAuth
    flow would produce a connection with no identity to revoke.
    """
    if agent_auth.user is None:
        raise HTTPException(
            status_code=403,
            detail="Connecting an account requires a signed-in user, not an API key.",
        )
    return agent_auth.user


def _require_provider(provider_id: str):
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider_id}'.")
    configured, reason = is_configured(provider_id)
    if not configured:
        raise HTTPException(
            status_code=503, detail={"error": "provider_not_configured", "message": reason}
        )
    return provider


def _sweep_expired(session: Session) -> None:
    cutoff = _utcnow() - STATE_TTL
    for row in session.exec(
        select(OAuthConnectState).where(OAuthConnectState.created_at < cutoff)
    ).all():
        session.delete(row)


def _safe_redirect(target: str | None) -> str:
    """Only ever send the browser back inside our own UI.

    An open redirect on a callback is how an OAuth flow becomes a phishing
    hop, so anything that is not a plain relative path is discarded rather
    than sanitized.
    """
    base = settings.ui_base_url.rstrip("/")
    if not target or not target.startswith("/") or target.startswith("//"):
        return f"{base}/settings"
    return f"{base}{target}"


def _ui_redirect(target: str | None, *, provider: str, status: str, detail: str = "") -> str:
    url = _safe_redirect(target)
    separator = "&" if "?" in url else "?"
    query = f"connection={quote(provider)}&connection_status={quote(status)}"
    if detail:
        query += f"&connection_error={quote(detail[:200])}"
    return f"{url}{separator}{query}"


class AuthorizeRequest(BaseModel):
    # Relative UI path to return to, e.g. "/integrations/mcp-builder".
    redirect_after: str | None = Field(default=None, max_length=300)


class PollRequest(BaseModel):
    state: str = Field(min_length=1, max_length=200)


@router.get("")
def list_provider_connections(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Every provider this build knows, whether it is usable, and who is connected."""
    user = _require_user(agent_auth)
    existing = {
        connection.provider: connection
        for connection in list_connections(session, org_id=agent_auth.org.id, user_id=user.id)
    }

    entries = []
    for provider_id in ALL_PROVIDERS:
        provider = get_provider(provider_id)
        assert provider is not None
        configured, reason = is_configured(provider_id)
        connection = existing.get(provider_id)
        entries.append(
            {
                "id": provider.id,
                "display_name": provider.display_name,
                "kind": provider.kind,
                "flow": provider.flow,
                "scopes": provider.scopes,
                "configured": configured,
                "reason": reason,
                "setup_url": provider.setup_url,
                "grant_summary": provider.grant_summary,
                "callback_url": callback_url(provider.id) if provider.flow != "device" else None,
                "connection": serialize(connection) if connection else None,
            }
        )
    return {"providers": entries}


@router.post("/{provider_id}/authorize")
async def authorize(
    provider_id: str,
    body: AuthorizeRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Begin a connection. Returns a URL to visit, or a device code to show."""
    user = _require_user(agent_auth)
    provider = _require_provider(provider_id)
    ensure_agent_can(session, agent_auth, _PERMISSION[provider_id])
    _sweep_expired(session)

    state = secrets.token_urlsafe(32)

    if provider.flow == "device":
        try:
            device = await start_device_authorization(
                region=settings.aws_sso_region, start_url=settings.aws_sso_start_url
            )
        except ConnectError as exc:
            raise _connect_error(exc)
        session.add(
            OAuthConnectState(
                state=state,
                provider=provider_id,
                org_id=agent_auth.org.id,
                user_id=user.id,
                redirect_after=body.redirect_after,
                device_payload_json=json.dumps(
                    {
                        "client_id": device.client_id,
                        "client_secret": device.client_secret,
                        "device_code": device.device_code,
                    }
                ),
            )
        )
        session.commit()
        return {
            "flow": "device",
            "state": state,
            "user_code": device.user_code,
            "verification_uri": device.verification_uri,
            "verification_uri_complete": device.verification_uri_complete,
            "interval": device.interval,
            "expires_in": device.expires_in,
            "grant_summary": provider.grant_summary,
        }

    verifier, challenge = pkce_pair()
    session.add(
        OAuthConnectState(
            state=state,
            provider=provider_id,
            org_id=agent_auth.org.id,
            user_id=user.id,
            code_verifier=verifier,
            redirect_after=body.redirect_after,
        )
    )
    session.commit()
    return {
        "flow": "authorization_code",
        "authorization_url": authorization_url(provider, state=state, code_challenge=challenge),
        "grant_summary": provider.grant_summary,
    }


@router.get("/{provider_id}/callback")
async def oauth_callback(
    provider_id: str,
    state: str = Query(...),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Where the provider sends the browser back. No sutr session on it."""
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider_id}'.")

    pending = session.exec(
        select(OAuthConnectState).where(OAuthConnectState.state == state)
    ).first()
    if pending is None or pending.provider != provider_id:
        # Nothing to attribute this code to; do not guess.
        return RedirectResponse(
            _ui_redirect(None, provider=provider_id, status="error", detail="invalid_state"),
            status_code=302,
        )

    org_id, user_id = pending.org_id, pending.user_id
    verifier = pending.code_verifier
    redirect_after = pending.redirect_after
    started = (
        pending.created_at
        if pending.created_at.tzinfo
        else pending.created_at.replace(tzinfo=timezone.utc)
    )
    session.delete(pending)
    session.commit()

    if started < _utcnow() - STATE_TTL:
        return RedirectResponse(
            _ui_redirect(
                redirect_after, provider=provider_id, status="error", detail="authorization_expired"
            ),
            status_code=302,
        )
    if error or not code:
        detail = error_description or error or "missing_code"
        logger.info("connection aborted for %s: %s", provider_id, detail)
        return RedirectResponse(
            _ui_redirect(redirect_after, provider=provider_id, status="denied", detail=detail),
            status_code=302,
        )

    try:
        payload = await exchange_code(provider, code=code, code_verifier=verifier)
        label = await fetch_account_label(provider, payload)
    except ConnectError as exc:
        logger.warning("connection failed for %s: %s", provider_id, exc.message)
        return RedirectResponse(
            _ui_redirect(redirect_after, provider=provider_id, status="error", detail=exc.message),
            status_code=302,
        )

    connection = save_connection(
        session,
        org_id=org_id,
        user_id=user_id,
        provider=provider_id,
        access_token_value=payload["access_token"],
        refresh_token_value=payload.get("refresh_token"),
        scopes=payload.get("scope") or provider.scopes,
        account_label=label,
        expires=expires_at(payload),
        metadata={},
    )
    record_audit(
        session,
        org_id=org_id,
        action="connection.created",
        summary=f"{provider.display_name} account connected" + (f" ({label})" if label else ""),
        target_type="provider_connection",
        target_id=str(connection.id),
        actor_type="user",
        actor_user_id=user_id,
        metadata={"provider": provider_id, "scopes": connection.scopes},
    )
    session.commit()
    return RedirectResponse(
        _ui_redirect(redirect_after, provider=provider_id, status="connected"), status_code=302
    )


@router.post("/aws/poll")
async def poll_aws_device(
    body: PollRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """One step of the AWS device grant. `pending` is a normal answer."""
    user = _require_user(agent_auth)
    provider = _require_provider("aws")
    ensure_agent_can(session, agent_auth, "deployments:manage")

    pending = session.exec(
        select(OAuthConnectState).where(OAuthConnectState.state == body.state)
    ).first()
    if pending is None or pending.provider != "aws":
        raise HTTPException(status_code=404, detail="No AWS authorization is in flight.")
    if pending.user_id != user.id or pending.org_id != agent_auth.org.id:
        # The state is a bearer-ish secret; refuse cross-user polling outright.
        raise HTTPException(status_code=404, detail="No AWS authorization is in flight.")

    device = json.loads(pending.device_payload_json)
    try:
        payload = await poll_device_token(
            region=settings.aws_sso_region,
            client_id=device["client_id"],
            client_secret=device["client_secret"],
            device_code=device["device_code"],
        )
    except ConnectError as exc:
        session.delete(pending)
        session.commit()
        raise _connect_error(exc)

    if payload is None:
        return {"status": "pending"}

    redirect_after = pending.redirect_after
    session.delete(pending)

    connection = save_connection(
        session,
        org_id=agent_auth.org.id,
        user_id=user.id,
        provider="aws",
        access_token_value=payload["accessToken"],
        refresh_token_value=payload.get("refreshToken"),
        scopes=provider.scopes,
        account_label=settings.aws_sso_start_url,
        expires=expires_at({"expires_in": payload.get("expiresIn")}),
        metadata={"sso_region": settings.aws_sso_region, "start_url": settings.aws_sso_start_url},
    )
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="connection.created",
        summary="AWS IAM Identity Center account connected",
        target_type="provider_connection",
        target_id=str(connection.id),
        metadata={"provider": "aws", "sso_region": settings.aws_sso_region},
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    session.refresh(connection)
    return {
        "status": "connected",
        "connection": serialize(connection),
        "redirect_after": redirect_after,
    }


@router.get("/github/repos")
async def github_repos(
    q: str = Query(default="", max_length=120),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Repositories the connected GitHub account can read."""
    user = _require_user(agent_auth)
    ensure_agent_can(session, agent_auth, "integrations:manage")
    connection = find_connection(
        session, org_id=agent_auth.org.id, user_id=user.id, provider="github"
    )
    if connection is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_connected",
                "message": "Connect a GitHub account first, or paste a repository URL.",
            },
        )
    try:
        token = await access_token(session, connection)
        repos = await list_github_repositories(token, query=q)
    except ConnectError as exc:
        raise _connect_error(exc)
    except OpenAPIError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code, "message": exc.message})
    return {"account": connection.account_label, "repositories": repos}


@router.get("/{connection_id}/targets")
async def deploy_targets(
    connection_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Projects / subscriptions / accounts this connection can deploy into."""
    user = _require_user(agent_auth)
    connection = load_connection(session, connection_id, org_id=agent_auth.org.id)
    if connection is None or connection.user_id != user.id:
        raise HTTPException(status_code=404, detail="Connection not found")
    permission = _PERMISSION.get(connection.provider, "deployments:manage")
    ensure_agent_can(session, agent_auth, permission)

    # Checked before a token is minted: "GitHub has no deploy targets" is a
    # question about the provider, not about the credential's health, and
    # answering it with a token error would send the user to fix the wrong
    # thing.
    if connection.provider not in ("gcp", "azure", "aws"):
        raise HTTPException(
            status_code=400,
            detail=f"'{connection.provider}' is not a deployment target.",
        )

    try:
        token = await access_token(session, connection)
        if connection.provider == "gcp":
            targets = await gcp_projects(token)
        elif connection.provider == "azure":
            targets = await azure_subscriptions(token)
        else:
            region = metadata_of(connection).get("sso_region") or settings.aws_sso_region
            targets = await aws_accounts(token, region)
    except ConnectError as exc:
        raise _connect_error(exc)
    return {"provider": connection.provider, "targets": targets}


@router.delete("/{connection_id}", status_code=204)
def disconnect(
    connection_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> None:
    """Revoke locally: the tokens are destroyed, the provider grant is not.

    Only the user can revoke the app itself, on the provider's own settings
    page. Saying so in the UI matters - "Disconnected" that leaves a live
    grant behind would be a lie.
    """
    user = _require_user(agent_auth)
    connection = load_connection(session, connection_id, org_id=agent_auth.org.id)
    if connection is None or connection.user_id != user.id:
        raise HTTPException(status_code=404, detail="Connection not found")
    permission = _PERMISSION.get(connection.provider, "deployments:manage")
    ensure_agent_can(session, agent_auth, permission)

    provider = get_provider(connection.provider)
    label = connection.account_label
    delete_connection(session, connection)
    ip, user_agent = request_meta(request)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="connection.deleted",
        summary=f"{provider.display_name if provider else connection.provider} account "
        f"disconnected" + (f" ({label})" if label else ""),
        target_type="provider_connection",
        target_id=str(connection_id),
        ip=ip,
        user_agent=user_agent,
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()


__all__ = ["router"]
