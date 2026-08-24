"""AWS authorization: the OAuth 2.0 device grant against IAM Identity Center.

AWS publishes no OAuth interface to its own service APIs, so "connect AWS with
OAuth" resolves to the one place AWS really does speak OAuth: the ``sso-oidc``
endpoint behind IAM Identity Center, which implements RFC 8628. The shape is:

    RegisterClient          -> a throwaway public client for this org
    StartDeviceAuthorization-> user_code + verification URI shown to the user
    CreateToken (polled)    -> an Identity Center access token
    ListAccounts / ListAccountRoles / GetRoleCredentials
                            -> short-lived AKID/secret/session-token

Only the Identity Center token is stored. The AWS credentials it exchanges for
last an hour at most and are fetched per operation, which is the whole reason
to prefer this over a stored access key pair.

These endpoints are unauthenticated (register, device authorization, token) or
bearer-authenticated (the portal). None of them are SigV4-signed - that starts
only once we hold role credentials, in `deploy/aws/sigv4.py`.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from sutr.connections.errors import ConnectError

REQUEST_TIMEOUT_SECONDS = 20
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_NAME = "sutr-deployments"


def oidc_endpoint(region: str) -> str:
    return f"https://oidc.{region}.amazonaws.com"


def portal_endpoint(region: str) -> str:
    return f"https://portal.sso.{region}.amazonaws.com"


@dataclass
class DeviceAuthorization:
    """What the user has to act on, plus what we need to poll with."""

    client_id: str
    client_secret: str
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


async def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise ConnectError("fetch_failed", f"Could not reach AWS IAM Identity Center: {exc}")
    return _read(response)


async def _get(url: str, token: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            response = await client.get(
                url, params=params, headers={"x-amz-sso_bearer_token": token}
            )
    except httpx.HTTPError as exc:
        raise ConnectError("fetch_failed", f"Could not reach the AWS access portal: {exc}")
    return _read(response)


def _read(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_success:
        return payload

    # sso-oidc reports RFC 8628 conditions in `error`; everything else in
    # `__type` / `message`. Both are surfaced, because "AWS said no" is not a
    # diagnosis.
    error = payload.get("error") or payload.get("__type") or f"HTTP {response.status_code}"
    message = payload.get("error_description") or payload.get("message") or ""
    if response.status_code == 401:
        raise ConnectError(
            "aws_session_expired",
            "The IAM Identity Center session has expired. Reconnect AWS.",
        )
    raise ConnectError(f"aws_{_slug(error)}", f"AWS returned {error}. {message}".strip())


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(value)).strip("_").lower()[:40]


async def start_device_authorization(*, region: str, start_url: str) -> DeviceAuthorization:
    """Register a client and open a device authorization.

    A fresh client is registered per attempt rather than cached: the
    registration is free, expires on its own, and caching it would mean
    storing a second long-lived secret to save one HTTP call.
    """
    registration = await _post(
        f"{oidc_endpoint(region)}/client/register",
        {"clientName": CLIENT_NAME, "clientType": "public", "scopes": ["sso:account:access"]},
    )
    client_id = registration.get("clientId")
    client_secret = registration.get("clientSecret")
    if not client_id or not client_secret:
        raise ConnectError("aws_register_failed", "AWS did not return a client registration.")

    device = await _post(
        f"{oidc_endpoint(region)}/device_authorization",
        {"clientId": client_id, "clientSecret": client_secret, "startUrl": start_url},
    )
    if not device.get("deviceCode") or not device.get("verificationUriComplete"):
        raise ConnectError(
            "aws_device_failed",
            "AWS did not return a device authorization. Check that the start URL "
            "is your IAM Identity Center portal URL.",
        )
    return DeviceAuthorization(
        client_id=client_id,
        client_secret=client_secret,
        device_code=device["deviceCode"],
        user_code=device.get("userCode", ""),
        verification_uri=device.get("verificationUri", ""),
        verification_uri_complete=device["verificationUriComplete"],
        expires_in=int(device.get("expiresIn") or 600),
        interval=int(device.get("interval") or 5),
    )


async def poll_device_token(
    *, region: str, client_id: str, client_secret: str, device_code: str
) -> dict[str, Any] | None:
    """One poll. None means "still waiting", which is not an error.

    RFC 8628 defines `authorization_pending` and `slow_down` as normal states
    of a flow in progress; only a real refusal raises.
    """
    try:
        return await _post(
            f"{oidc_endpoint(region)}/token",
            {
                "clientId": client_id,
                "clientSecret": client_secret,
                "grantType": DEVICE_GRANT,
                "deviceCode": device_code,
            },
        )
    except ConnectError as exc:
        if exc.code in ("aws_authorizationpendingexception", "aws_authorization_pending"):
            return None
        if exc.code in ("aws_slowdownexception", "aws_slow_down"):
            return None
        raise


async def list_accounts(*, region: str, token: str) -> list[dict[str, Any]]:
    """Accounts the connected identity is assigned to, with their roles.

    Pre-joining roles onto accounts costs one request per account but turns
    the UI into a single flat picker: an account with no role assignment is
    not a deployable target, and finding that out at deploy time is too late.
    """
    payload = await _get(
        f"{portal_endpoint(region)}/assignment/accounts", token, {"max_result": 100}
    )
    accounts: list[dict[str, Any]] = []
    for entry in payload.get("accountList", []):
        account_id = entry.get("accountId")
        if not account_id:
            continue
        roles = await _get(
            f"{portal_endpoint(region)}/assignment/roles",
            token,
            {"account_id": account_id, "max_result": 100},
        )
        accounts.append(
            {
                "account_id": account_id,
                "account_name": entry.get("accountName") or account_id,
                "email": entry.get("emailAddress") or "",
                "roles": [
                    role.get("roleName")
                    for role in roles.get("roleList", [])
                    if role.get("roleName")
                ],
            }
        )
    return accounts


@dataclass
class RoleCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    # Milliseconds since the epoch, as AWS reports it.
    expiration: int


async def get_role_credentials(
    *, region: str, token: str, account_id: str, role_name: str
) -> RoleCredentials:
    payload = await _get(
        f"{portal_endpoint(region)}/federation/credentials",
        token,
        {"account_id": account_id, "role_name": role_name},
    )
    creds = payload.get("roleCredentials") or {}
    if not creds.get("accessKeyId"):
        raise ConnectError(
            "aws_no_role_credentials",
            f"IAM Identity Center did not issue credentials for {role_name} in "
            f"{account_id}. Check that the role is still assigned to you.",
        )
    return RoleCredentials(
        access_key_id=creds["accessKeyId"],
        secret_access_key=creds.get("secretAccessKey", ""),
        session_token=creds.get("sessionToken", ""),
        expiration=int(creds.get("expiration") or 0),
    )
