"""The AWS device grant against IAM Identity Center.

The property that matters most here: `authorization_pending` and `slow_down`
are *normal states of a flow in progress*, not failures. Treating either as an
error would abort every flow the moment the user takes more than five seconds
to read the code.
"""

import httpx
import pytest

from sutr.connections.device import (
    get_role_credentials,
    list_accounts,
    poll_device_token,
    start_device_authorization,
)
from sutr.connections.errors import ConnectError

REGION = "us-east-1"
START_URL = "https://d-1234567890.awsapps.com/start"


def _mock(monkeypatch, handler):
    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.connections.device.httpx.AsyncClient", Patched)


async def test_start_registers_a_client_then_opens_a_device_authorization(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/client/register":
            return httpx.Response(200, json={"clientId": "cid", "clientSecret": "csecret"})
        return httpx.Response(
            200,
            json={
                "deviceCode": "dev-code",
                "userCode": "ABCD-EFGH",
                "verificationUri": "https://device.sso.us-east-1.amazonaws.com/",
                "verificationUriComplete": "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH",
                "expiresIn": 600,
                "interval": 5,
            },
        )

    _mock(monkeypatch, handler)
    authorization = await start_device_authorization(region=REGION, start_url=START_URL)
    assert seen == ["/client/register", "/device_authorization"]
    assert authorization.user_code == "ABCD-EFGH"
    assert authorization.device_code == "dev-code"
    assert authorization.interval == 5


async def test_a_bad_start_url_is_explained(monkeypatch):
    def handler(request):
        if request.url.path == "/client/register":
            return httpx.Response(200, json={"clientId": "c", "clientSecret": "s"})
        return httpx.Response(400, json={"error": "invalid_request"})

    _mock(monkeypatch, handler)
    with pytest.raises(ConnectError) as excinfo:
        await start_device_authorization(region=REGION, start_url="https://wrong.example.com")
    assert "invalid_request" in excinfo.value.message


@pytest.mark.parametrize("error", ["AuthorizationPendingException", "SlowDownException"])
async def test_pending_and_slow_down_are_not_failures(monkeypatch, error):
    _mock(monkeypatch, lambda request: httpx.Response(400, json={"error": error}))
    assert (
        await poll_device_token(region=REGION, client_id="c", client_secret="s", device_code="d")
        is None
    )


async def test_a_real_refusal_raises(monkeypatch):
    _mock(
        monkeypatch,
        lambda request: httpx.Response(400, json={"error": "AccessDeniedException"}),
    )
    with pytest.raises(ConnectError):
        await poll_device_token(region=REGION, client_id="c", client_secret="s", device_code="d")


async def test_a_completed_poll_returns_the_token(monkeypatch):
    _mock(
        monkeypatch,
        lambda request: httpx.Response(200, json={"accessToken": "sso-token", "expiresIn": 28800}),
    )
    payload = await poll_device_token(
        region=REGION, client_id="c", client_secret="s", device_code="d"
    )
    assert payload["accessToken"] == "sso-token"


async def test_accounts_carry_their_roles(monkeypatch):
    """Pre-joining roles means an account with no assignment cannot be picked
    as a target and then fail minutes into a build."""

    def handler(request):
        if request.url.path == "/assignment/accounts":
            assert request.headers["x-amz-sso_bearer_token"] == "sso-token"
            return httpx.Response(
                200,
                json={
                    "accountList": [
                        {
                            "accountId": "111122223333",
                            "accountName": "Prod",
                            "emailAddress": "a@b.c",
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"roleList": [{"roleName": "AdministratorAccess"}, {"roleName": "ReadOnly"}]}
        )

    _mock(monkeypatch, handler)
    accounts = await list_accounts(region=REGION, token="sso-token")
    assert accounts == [
        {
            "account_id": "111122223333",
            "account_name": "Prod",
            "email": "a@b.c",
            "roles": ["AdministratorAccess", "ReadOnly"],
        }
    ]


async def test_role_credentials_are_returned_for_signing(monkeypatch):
    _mock(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "roleCredentials": {
                    "accessKeyId": "ASIA...",
                    "secretAccessKey": "secret",
                    "sessionToken": "session",
                    "expiration": 1_700_000_000_000,
                }
            },
        ),
    )
    credentials = await get_role_credentials(
        region=REGION, token="sso-token", account_id="111122223333", role_name="AdministratorAccess"
    )
    assert credentials.access_key_id == "ASIA..."
    assert credentials.session_token == "session"


async def test_an_expired_sso_session_says_so(monkeypatch):
    _mock(
        monkeypatch,
        lambda request: httpx.Response(401, json={"message": "Session token not found"}),
    )
    with pytest.raises(ConnectError) as excinfo:
        await list_accounts(region=REGION, token="stale")
    assert excinfo.value.code == "aws_session_expired"
