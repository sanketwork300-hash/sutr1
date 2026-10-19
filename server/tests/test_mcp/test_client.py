import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sutr.mcp.client import (
    UpstreamRedirectRefusedError,
    _auth_headers,
    call_tool,
    http_client_factory,
    list_tools,
)
from sutr.models.integration import InstalledIntegration
from sutr.models.oauth import OAuthState


def _make_installed(auth_method="token", token_secret_id=None):
    return InstalledIntegration(
        org_id=uuid.uuid4(),
        integration_id="posthog",
        type="remote_mcp",
        url="https://mcp.posthog.com/mcp",
        auth_method=auth_method,
        token_secret_id=token_secret_id,
    )


def test_auth_headers_token():
    secret_id = uuid.uuid4()
    installed = _make_installed(auth_method="token", token_secret_id=secret_id)
    with patch("sutr.mcp.client.get_secret_value", return_value="my_token"):
        headers = _auth_headers(installed)
    assert headers == {"Authorization": "Bearer my_token"}


def test_auth_headers_oauth():
    secret_id = uuid.uuid4()
    installed = _make_installed(auth_method="oauth")
    oauth_state = OAuthState(
        org_id=uuid.uuid4(),
        integration_id="test",
        access_token_secret_id=secret_id,
    )
    with patch("sutr.mcp.client.get_secret_value", return_value="oauth_token"):
        headers = _auth_headers(installed, oauth_state)
    assert headers == {"Authorization": "Bearer oauth_token"}


def test_auth_headers_no_credentials():
    installed = _make_installed(auth_method="oauth")
    headers = _auth_headers(installed)
    assert headers == {}


@pytest.mark.anyio
async def test_list_tools_calls_mcp():
    installed = _make_installed()

    mock_tool = MagicMock()
    mock_tool.model_dump.return_value = {"name": "test_tool", "description": "A tool"}

    mock_result = MagicMock()
    mock_result.tools = [mock_tool]

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=mock_result)

    with (
        patch("sutr.mcp.client.streamablehttp_client") as mock_client,
        patch("sutr.mcp.client.ClientSession") as mock_session_cls,
        patch("sutr.mcp.client.get_secret_value", return_value="tok"),
    ):
        mock_client.return_value.__aenter__ = AsyncMock(
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await list_tools(installed)

    assert len(result) == 1
    assert result[0]["name"] == "test_tool"


@pytest.mark.anyio
async def test_call_tool_calls_mcp():
    installed = _make_installed()

    mock_content = MagicMock()
    mock_content.model_dump.return_value = {"type": "text", "text": "result"}

    mock_result = MagicMock()
    mock_result.content = [mock_content]
    mock_result.isError = False

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.call_tool = AsyncMock(return_value=mock_result)

    with (
        patch("sutr.mcp.client.streamablehttp_client") as mock_client,
        patch("sutr.mcp.client.ClientSession") as mock_session_cls,
        patch("sutr.mcp.client.get_secret_value", return_value="tok"),
    ):
        mock_client.return_value.__aenter__ = AsyncMock(
            return_value=(MagicMock(), MagicMock(), MagicMock())
        )
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await call_tool(installed, "test_tool", {"arg": "value"})

    assert result["isError"] is False
    assert result["content"][0]["text"] == "result"


# ── S21: the SDK's httpx client does not follow redirects ───────────────────


def _redirecting_client_class(sent: list):
    def handler(request):
        sent.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return Patched


@pytest.mark.anyio
async def test_the_mcp_http_client_refuses_a_redirect(monkeypatch):
    """A screened MCP URL answering `302 Location: http://127.0.0.1/` must
    not have the Location opened: the SDK's own factory always follows."""
    sent: list = []
    monkeypatch.setattr("sutr.mcp.client.httpx.AsyncClient", _redirecting_client_class(sent))

    client = http_client_factory(headers={"Authorization": "Bearer t"})
    assert client.follow_redirects is False
    assert client.headers["authorization"] == "Bearer t"

    async with client:
        with pytest.raises(UpstreamRedirectRefusedError, match="redirect"):
            await client.get("https://mcp.example.com/mcp")

    assert [str(r.url) for r in sent] == ["https://mcp.example.com/mcp"]


def test_the_mcp_http_client_keeps_the_sdk_timeouts_and_auth():
    timeout = httpx.Timeout(7.0, read=70.0)
    auth = httpx.BasicAuth("u", "p")
    client = http_client_factory(timeout=timeout, auth=auth)
    assert client.timeout == timeout
    assert client.auth is auth
    assert client.follow_redirects is False

    default = http_client_factory()
    assert default.timeout.read == 300.0


@pytest.mark.anyio
async def test_every_connection_uses_the_redirect_refusing_factory():
    installed = _make_installed()
    seen: dict = {}

    class _Streams:
        async def __aenter__(self):
            return (MagicMock(), MagicMock(), MagicMock())

        async def __aexit__(self, *exc):
            return False

    def fake_streamable(url, **kwargs):
        seen.update(kwargs)
        return _Streams()

    mock_session = AsyncMock()
    mock_session.initialize = AsyncMock()
    mock_session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))
    with (
        patch("sutr.mcp.client.streamablehttp_client", fake_streamable),
        patch("sutr.mcp.client.ClientSession") as mock_session_cls,
        patch("sutr.mcp.client.get_secret_value", return_value="tok"),
    ):
        mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        await list_tools(installed)

    assert seen["httpx_client_factory"] is http_client_factory
