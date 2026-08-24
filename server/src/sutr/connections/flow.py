"""The authorization-code half of connected accounts (GitHub, GCP, Azure).

Provider differences are read from `providers.py`; this module only knows the
grant. Three things it deliberately does:

- PKCE on every provider that tolerates it, because the callback lands on a
  plain browser redirect with no session attached.
- Token responses are parsed from JSON *or* form encoding: GitHub returns
  ``application/x-www-form-urlencoded`` unless asked otherwise, and a silent
  parse failure here would look like a rejected grant.
- Errors carry the provider's own ``error_description`` when it sends one.
  "Token exchange failed" tells nobody anything.
"""

import base64
import hashlib
import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx

from sutr.connections.errors import ConnectError
from sutr.connections.providers import (
    OAuthProvider,
    callback_url,
    client_credentials,
)

TOKEN_TIMEOUT_SECONDS = 20
# Refresh this far before real expiry: a token that expires mid-deploy fails
# the deploy, not the refresh.
EXPIRY_SKEW = timedelta(seconds=120)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pkce_pair() -> tuple[str, str]:
    verifier = "".join(
        secrets.choice(string.ascii_letters + string.digits + "-._~") for _ in range(96)
    )
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(provider: OAuthProvider, *, state: str, code_challenge: str) -> str:
    client_id, _ = client_credentials(provider.id)
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": callback_url(provider.id),
        "scope": provider.scopes,
        "state": state,
        **provider.extra_authorize_params,
    }
    if provider.uses_pkce and code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{provider.authorize_url}?{urlencode(params)}"


def _parse_token_response(response: httpx.Response) -> dict[str, Any]:
    """JSON if it is JSON, form encoding otherwise (GitHub's default)."""
    body = response.text
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return response.json()
        except ValueError:
            raise ConnectError("token_exchange_failed", "The provider returned malformed JSON.")
    parsed = dict(parse_qsl(body))
    if parsed:
        return parsed
    try:
        return json.loads(body)
    except ValueError:
        raise ConnectError(
            "token_exchange_failed",
            "The provider returned a token response sutr could not read.",
        )


def _raise_for_oauth_error(payload: dict[str, Any]) -> None:
    error = payload.get("error")
    if not error:
        return
    description = payload.get("error_description") or ""
    raise ConnectError(
        "token_exchange_failed",
        f"The provider refused the authorization ({error}). {description}".strip(),
    )


async def _post_token(provider: OAuthProvider, data: dict[str, str]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=TOKEN_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.post(
                provider.token_url,
                data=data,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
    except httpx.HTTPError as exc:
        raise ConnectError("fetch_failed", f"Could not reach {provider.display_name}: {exc}")

    payload = _parse_token_response(response)
    _raise_for_oauth_error(payload)
    if not response.is_success:
        raise ConnectError(
            "token_exchange_failed",
            f"{provider.display_name} returned HTTP {response.status_code} for the token request.",
        )
    if not payload.get("access_token"):
        raise ConnectError(
            "token_exchange_failed",
            f"{provider.display_name} did not return an access token.",
        )
    return payload


async def exchange_code(
    provider: OAuthProvider, *, code: str, code_verifier: str
) -> dict[str, Any]:
    client_id, client_secret = client_credentials(provider.id)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": callback_url(provider.id),
    }
    if provider.uses_pkce and code_verifier:
        data["code_verifier"] = code_verifier
    return await _post_token(provider, data)


async def refresh_access_token(provider: OAuthProvider, refresh_token: str) -> dict[str, Any]:
    client_id, client_secret = client_credentials(provider.id)
    return await _post_token(
        provider,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            # Azure re-issues per resource; asking for the same scopes keeps
            # the ARM audience on the refreshed token.
            "scope": provider.scopes,
        },
    )


def expires_at(payload: dict[str, Any]) -> datetime | None:
    """When the access token dies, or None if the provider says it does not.

    GitHub OAuth-app tokens have no expiry and omit the field; treating a
    missing `expires_in` as "expires now" would refresh on every call.
    """
    raw = payload.get("expires_in")
    if raw in (None, ""):
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    return utcnow() + timedelta(seconds=seconds)


def _id_token_claims(payload: dict[str, Any]) -> dict[str, Any]:
    """Read the unverified claims of an id_token, for display only.

    Not verified, and never used for authorization: the token arrived over TLS
    from the provider's own token endpoint in direct response to our request,
    and the only thing taken from it is a label to print next to a Disconnect
    button.
    """
    token = payload.get("id_token")
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    body = token.split(".")[1]
    body += "=" * (-len(body) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(body))
    except (ValueError, json.JSONDecodeError):
        return {}


async def fetch_account_label(provider: OAuthProvider, payload: dict[str, Any]) -> str:
    """A human-readable name for the connected identity.

    Cosmetic by design. A provider that will not tell us who it authorized
    still gets connected: the grant is what matters.
    """
    if provider.id == "github":
        try:
            async with httpx.AsyncClient(
                timeout=TOKEN_TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                response = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {payload['access_token']}",
                        "User-Agent": "sutr-connections",
                    },
                )
            if response.is_success:
                return str(response.json().get("login") or "")
        except (httpx.HTTPError, ValueError):
            return ""
        return ""

    claims = _id_token_claims(payload)
    for key in ("email", "preferred_username", "upn", "unique_name", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
