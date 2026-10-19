"""Connected accounts: authorize sutr against GitHub, Google Cloud, Azure, AWS.

One resource for both uses, because they are the same grant with different
consequences: GitHub authorizes *reading a specification*, the cloud providers
authorize *running a server*. Permissions differ accordingly - a source
connection needs `integrations:manage`, a deploy connection needs
`deployments:manage`.

The callback is the delicate part. It arrives as a bare browser redirect with
no sutr session on it, so the `state` row binds the code to a user, and a
cookie set when the flow started binds the row to the browser that asked for
it. Without the cookie, anyone holding the consent URL could complete the flow
and have *their* tokens stored on the requester's connection. The row is
single-use, TTL-bounded, and deleted before the code is exchanged, whatever
the outcome.
"""

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
from sutr.observability.log_safety import single_line
from sutr.openapi.errors import OpenAPIError
from sutr.openapi.sources import list_github_repositories
from sutr.services.audit import actor_from_agent_auth, record_audit, request_meta
from sutr.ui_redirect import is_safe_ui_path, safe_ui_path, ui_url

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


# ── browser binding ──────────────────────────────────────────────────────────
#
# `authorize` sets a cookie carrying an HMAC of the state, and `oauth_callback`
# requires it. The provider's redirect is a top-level GET, which SameSite=Lax
# still sends, while a consent URL pasted into someone else's browser carries
# no cookie at all. The key is derived from the JWT secret with its own HKDF
# info string so a cookie value can never double as anything else.

CONNECT_COOKIE = "sutr_connect"
_CONNECT_COOKIE_INFO = b"sutr-connect-cookie"
_CONNECT_COOKIE_PATH = "/api/connections"


def _hkdf_sha256(secret: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 with an empty salt (RFC 5869)."""
    prk = hmac.new(b"\x00" * 32, secret, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _connect_cookie_value(state: str) -> str:
    key = _hkdf_sha256(settings.jwt_secret_key.encode("utf-8"), _CONNECT_COOKIE_INFO)
    return hmac.new(key, state.encode("utf-8"), hashlib.sha256).hexdigest()


def _cookie_is_secure() -> bool:
    return settings.base_url.lower().startswith("https://")


def _set_connect_cookie(response: Response, state: str) -> None:
    response.set_cookie(
        CONNECT_COOKIE,
        _connect_cookie_value(state),
        max_age=int(STATE_TTL.total_seconds()),
        path=_CONNECT_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(),
    )


def _clear_connect_cookie(response: Response) -> None:
    response.delete_cookie(
        CONNECT_COOKIE,
        path=_CONNECT_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(),
    )


def _browser_matches(request: Request, state: str) -> bool:
    presented = request.cookies.get(CONNECT_COOKIE)
    if not presented:
        return False
    return hmac.compare_digest(presented, _connect_cookie_value(state))


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


# Connected accounts live under Govern → Credentials in the console.
_DEFAULT_RETURN_PATH = "/app/credentials"


def _ui_redirect(target: str | None, *, provider: str, status: str, detail: str = "") -> str:
    """Only ever send the browser back inside our own UI.

    An open redirect on a callback is how an OAuth flow becomes a phishing
    hop, so anything that is not a plain relative path is discarded rather
    than sanitized (see `ui_redirect`).
    """
    query = {"connection": provider, "connection_status": status}
    if detail:
        query["connection_error"] = detail[:200]
    return ui_url(safe_ui_path(target, fallback=_DEFAULT_RETURN_PATH), query=query)


def _return_path(target: str | None) -> str | None:
    """The client's return path if it stays inside the UI; otherwise none.

    Vetted when stored as well as when used, because the device flow hands the
    stored value back to the client instead of redirecting to it.
    """
    return target if is_safe_ui_path(target) else None


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
    response: Response,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Begin a connection. Returns a URL to visit, or a device code to show.

    For the redirect flow the response also sets the browser-binding cookie
    the callback will demand. The device flow has no callback, so none.
    """
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
                redirect_after=_return_path(body.redirect_after),
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
            redirect_after=_return_path(body.redirect_after),
        )
    )
    session.commit()
    _set_connect_cookie(response, state)
    return {
        "flow": "authorization_code",
        "authorization_url": authorization_url(provider, state=state, code_challenge=challenge),
        "grant_summary": provider.grant_summary,
    }


@router.get("/{provider_id}/callback")
async def oauth_callback(
    provider_id: str,
    request: Request,
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

    def _back(target: str | None, *, status: str, detail: str = "") -> RedirectResponse:
        response = RedirectResponse(
            _ui_redirect(target, provider=provider.id, status=status, detail=detail),
            status_code=302,
        )
        # The cookie is for one flow; whatever happened, it is spent.
        _clear_connect_cookie(response)
        return response

    pending = session.exec(
        select(OAuthConnectState).where(OAuthConnectState.state == state)
    ).first()
    if pending is None or pending.provider != provider_id:
        # Nothing to attribute this code to; do not guess.
        return _back(None, status="error", detail="invalid_state")

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

    if not _browser_matches(request, state):
        # The consent URL was completed by a browser other than the one that
        # asked for it. Storing these tokens would hand this user's account to
        # whoever started the flow, so the whole attempt is discarded.
        logger.warning("connection callback for %s not bound to the starting browser", provider.id)
        return _back(redirect_after, status="error", detail="browser_mismatch")

    if started < _utcnow() - STATE_TTL:
        return _back(redirect_after, status="error", detail="authorization_expired")
    if error or not code:
        detail = error_description or error or "missing_code"
        logger.info("connection aborted for %s: %s", provider.id, single_line(detail))
        return _back(redirect_after, status="denied", detail=detail)

    try:
        payload = await exchange_code(provider, code=code, code_verifier=verifier)
        label = await fetch_account_label(provider, payload)
    except ConnectError as exc:
        logger.warning("connection failed for %s: %s", provider.id, single_line(exc.message))
        return _back(redirect_after, status="error", detail=exc.message)

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
    return _back(redirect_after, status="connected")


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
