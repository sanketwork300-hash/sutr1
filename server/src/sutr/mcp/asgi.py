import hashlib
import json
import logging
import uuid

import jwt
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from sqlmodel import Session, select

from sutr.config import settings
from sutr.db import engine
from sutr.dependencies import AgentAuth, default_membership
from sutr.mcp.server import (
    RequestMeta,
    _current_auth,
    _current_request_meta,
    _session_auth,
    session_manager,
)
from sutr.models.api_key import ApiKey, hash_api_key
from sutr.models.oauth_revoked_token import OAuthRevokedToken
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User

logger = logging.getLogger(__name__)


def _extract_header(scope: dict, header_name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == header_name:
            return value.decode("utf-8")
    return None


def _is_token_revoked(token: str) -> bool:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with Session(engine) as session:
        row = session.get(OAuthRevokedToken, token_hash)
        return row is not None


def _authenticate(scope: dict) -> AgentAuth | None:  # noqa: C901
    """Replicate get_agent_auth logic for raw ASGI scope.

    Bearer JWT (OAuth) is checked first so that clients sending both
    Authorization and X-Api-Key headers (e.g. Claude Code after OAuth) use
    the OAuth token rather than hitting the API-key path and failing.
    """
    # 1. Try Bearer JWT (checked before API key so OAuth tokens always win)
    auth_header = _extract_header(scope, b"authorization") or ""
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer ") :]
        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
                options={"verify_aud": False},
            )
        except jwt.PyJWTError as exc:
            logger.warning("MCP auth: JWT decode failed: %s", exc)
            return None

        # Reject refresh tokens
        if payload.get("token_use") == "refresh":
            logger.warning("MCP auth: refresh token presented on MCP endpoint")
            return None

        # Reject purpose-specific tokens that must not authenticate MCP calls
        # (e.g. email-verification session handles).
        token_use = payload.get("token_use")
        if token_use == "email_verification":
            logger.warning("MCP auth: email-verification token presented on MCP endpoint")
            return None

        # Reject revoked tokens — covers both OAuth access tokens and admin
        # impersonation tokens (both go through the shared revocation list).
        if token_use in ("mcp_access", "access", "impersonation") and _is_token_revoked(token):
            logger.warning("MCP auth: token is revoked")
            return None

        user_id = payload.get("sub")
        if user_id is None:
            logger.warning("MCP auth: token missing 'sub' claim")
            return None

        # OAuth access tokens minted by _issue_access_token carry
        # token_use="mcp_access" and embed the grant's org_id. Legacy tokens
        # using the bare "access" label (human login JWTs) are handled by the
        # membership-lookup branch below.
        if token_use == "mcp_access":
            if payload.get("aud") != f"{settings.base_url}/mcp":
                logger.warning("MCP auth: mcp_access token has invalid audience")
                return None
            org_id = payload.get("org_id")
            if not org_id:
                logger.warning("MCP auth: access token missing 'org_id' claim")
                return None
            with Session(engine) as session:
                user = session.get(User, uuid.UUID(user_id))
                if user is None or not user.is_active:
                    logger.warning("MCP auth: user %s not found or inactive", user_id)
                    return None
                if int(payload.get("tv") or 0) != int(user.token_version or 0):
                    # nosemgrep: python-logger-credential-disclosure
                    logger.warning("MCP auth: stale token version for user %s", user_id)
                    return None
                org = session.get(Org, uuid.UUID(org_id))
                if not org:
                    logger.warning("MCP auth: org %s not found", org_id)
                    return None
                membership = session.exec(
                    select(OrgMembership)
                    .where(OrgMembership.user_id == uuid.UUID(user_id))
                    .where(OrgMembership.org_id == uuid.UUID(org_id))
                ).first()
                if not membership:
                    logger.warning("MCP auth: no membership for user %s in org %s", user_id, org_id)
                    return None
                return AgentAuth(org=org, user=user, api_key=None)

        # Legacy JWTs (no token_use claim) and impersonation tokens both
        # resolve the target's org from membership. Impersonation tokens
        # additionally carry an `impersonator_sub` claim which we resolve
        # into the admin User row for audit tagging downstream.
        with Session(engine) as session:
            user = session.get(User, uuid.UUID(user_id))
            if user is None or not user.is_active:
                logger.warning("MCP auth: legacy JWT — user %s not found or inactive", user_id)
                return None
            if int(payload.get("tv") or 0) != int(user.token_version or 0):
                # nosemgrep: python-logger-credential-disclosure
                logger.warning("MCP auth: stale token version for user %s", user_id)
                return None
            membership = default_membership(session, user.id)
            if not membership:
                logger.warning("MCP auth: legacy JWT — no membership for user %s", user_id)
                return None
            org = session.get(Org, membership.org_id)
            if not org:
                logger.warning("MCP auth: legacy JWT — org not found for user %s", user_id)
                return None

            impersonator: User | None = None
            if token_use == "impersonation":
                imp_sub = payload.get("impersonator_sub")
                if imp_sub:
                    try:
                        admin = session.get(User, uuid.UUID(imp_sub))
                    except (ValueError, TypeError):
                        admin = None
                    if admin and admin.is_active and admin.is_admin:
                        impersonator = admin
            return AgentAuth(org=org, user=user, api_key=None, impersonator=impersonator)

    # 2. Try API key
    raw_key = _extract_header(scope, b"x-api-key")
    if raw_key:
        key_hash = hash_api_key(raw_key)
        with Session(engine) as session:
            api_key = session.exec(
                select(ApiKey).where(ApiKey.key_hash == key_hash).where(ApiKey.is_active == True)  # noqa: E712
            ).first()
            if not api_key:
                logger.warning("MCP auth: invalid API key")
                return None
            org = session.get(Org, api_key.org_id)
            if not org:
                logger.warning("MCP auth: org not found for API key")
                return None
            return AgentAuth(org=org, user=None, api_key=api_key)

    if not auth_header:
        logger.warning("MCP auth: no Authorization header")
    else:
        logger.warning(
            "MCP auth: Authorization header scheme is not Bearer (got %r)", auth_header[:40]
        )
    return None


async def _send_401(send):
    resource_metadata_url = f"{settings.base_url}/.well-known/oauth-protected-resource/mcp"
    body = json.dumps({"error": "Not authenticated"}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
                [
                    b"www-authenticate",
                    f'Bearer resource_metadata="{resource_metadata_url}"'.encode(),
                ],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


MCP_SESSION_ID_HEADER = b"mcp-session-id"

# session id -> principal key of the credential that opened the session.
#
# A StreamableHTTP session is a long-lived server task; the SDK only checks
# that later requests come from the session's creator when `scope["user"]` is
# an `AuthenticatedUser`, which `mcp_asgi_app` now populates. This table is the
# gateway's own copy of that binding, so the property does not hinge on an
# SDK internal: a session may only be driven by the principal that opened it,
# which makes a leaked or guessed session id worthless to anyone else and
# stops a revoked credential being kept alive through a different one.
_session_principals: dict[str, str] = {}


def principal_key(auth: AgentAuth) -> str:
    """Stable, distinct identity for session binding.

    An API key is its own principal (two keys of one org must not share
    sessions, and revoking one must not leave the other's sessions usable);
    a user token is the user *in* the org the token resolved to.
    """
    if auth.api_key is not None:
        return f"api_key:{auth.api_key.id}"
    return f"user:{auth.user.id}:org:{auth.org.id}"


def _authenticated_user(key: str) -> AuthenticatedUser:
    """What the SDK's `_session_owners` check compares across requests.

    `authorization_context()` reduces this to (client_id, issuer, subject);
    all three are populated so the comparison is never degraded to `None`.
    """
    return AuthenticatedUser(
        AccessToken(
            token="",
            client_id=key,
            scopes=[],
            subject=key,
            claims={"iss": settings.base_url},
        )
    )


async def _send_session_not_found(send) -> None:
    """Same answer the SDK gives for an unknown session, so a caller cannot
    tell "no such session" from "not your session"."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "server-error",
            "error": {"code": -32600, "message": "Session not found"},
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _prune_dead_sessions() -> None:
    """Drop bindings for sessions the SDK has already reaped or terminated."""
    live = getattr(session_manager, "_server_instances", None)
    if live is None:
        return
    for session_id in [sid for sid in _session_principals if sid not in live]:
        _session_principals.pop(session_id, None)
        _session_auth.pop(session_id, None)


def _bind_new_session(send, key: str, auth: AgentAuth):
    """Wrap `send` so the session id the SDK assigns in the response is bound
    to the principal that made the creating request."""

    async def send_wrapper(message) -> None:
        if message["type"] == "http.response.start":
            for header_name, header_value in message.get("headers", []):
                if header_name.lower() == MCP_SESSION_ID_HEADER:
                    session_id = header_value.decode("utf-8")
                    _session_principals[session_id] = key
                    _session_auth[session_id] = auth
                    break
        await send(message)

    return send_wrapper


async def mcp_asgi_app(scope, receive, send):
    """Auth-aware ASGI app that wraps the MCP session manager."""
    if scope["type"] != "http":
        # Non-HTTP scopes (lifespan, etc.) pass through
        await session_manager.handle_request(scope, receive, send)
        return

    auth = _authenticate(scope)
    if auth is None:
        await _send_401(send)
        return

    key = principal_key(auth)
    # Activates the SDK's own session-owner check (StreamableHTTPSessionManager
    # compares authorization_context(scope["user"]) with the session's creator).
    scope["user"] = _authenticated_user(key)

    session_id = _extract_header(scope, MCP_SESSION_ID_HEADER)
    if session_id is not None:
        owner = _session_principals.get(session_id)
        if owner is not None:
            if owner != key:
                logger.warning(
                    "MCP: refusing session %s: presented credential is not the one that created it",
                    session_id[:16],
                )
                await _send_session_not_found(send)
                return
            # Handlers on this session read the credential presented *now*,
            # not the one captured when the session was created.
            _session_auth[session_id] = auth
    else:
        _prune_dead_sessions()
        send = _bind_new_session(send, key, auth)

    client = scope.get("client")
    meta = RequestMeta(
        ip=client[0] if client else None,
        user_agent=_extract_header(scope, b"user-agent"),
    )
    auth_token = _current_auth.set(auth)
    meta_token = _current_request_meta.set(meta)
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        _current_auth.reset(auth_token)
        _current_request_meta.reset(meta_token)
