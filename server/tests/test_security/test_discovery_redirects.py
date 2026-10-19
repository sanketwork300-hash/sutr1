"""SECURITY_BUGS.md S21: OAuth discovery for a custom MCP server neither
follows redirects nor trusts the URLs the remote end hands back.

The server URL is screened once. Everything discovery learns after that —
the resource-metadata hint, the authorization server, the endpoints — is
written by the remote end, and each one is somewhere this server is about to
send a request. A screened public host answering `302 Location:
http://127.0.0.1/` or naming `http://127.0.0.1/` as its authorization server
is the same SSRF one hop later.
"""

import socket
import uuid

import httpx
import pytest

from sutr.auth_start import start_oauth_for_installed
from sutr.models.integration import InstalledIntegration
from sutr.upstream_safety import UnsafeUpstreamUrlError

PUBLIC = "93.184.216.34"
LOOPBACK_URL = "http://127.0.0.1/"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Names resolve to a public address; literals are judged as themselves."""
    real = socket.getaddrinfo

    def fake(hostname, port, *args, **kwargs):
        try:
            socket.inet_pton(socket.AF_INET, hostname)
        except OSError:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port))]
        return real(hostname, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class _RedirectingClient:
    """Probe answers 302 to loopback; metadata names loopback everywhere."""

    constructed: list[dict] = []
    requested: list[str] = []

    def __init__(self, *args, **kwargs):
        type(self).constructed.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, headers=None):
        type(self).requested.append(str(url))
        return httpx.Response(
            302,
            headers={"Location": LOOPBACK_URL},
            request=httpx.Request("GET", url, headers=headers),
        )

    async def send(self, request):
        type(self).requested.append(str(request.url))
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcp.example.com/mcp",
                    "authorization_servers": [LOOPBACK_URL],
                },
                request=request,
            )
        raise AssertionError(f"unexpected request to {request.url}")


def _custom_installed(test_org) -> InstalledIntegration:
    return InstalledIntegration(
        org_id=test_org.id,
        integration_id=f"custom_{uuid.uuid4().hex[:8]}",
        type="remote_mcp",
        url="https://mcp.example.com/mcp",
        auth_method="oauth",
    )


async def test_discovery_does_not_follow_the_probe_redirect_and_refuses_the_loopback_as(
    session, test_org, monkeypatch
):
    _RedirectingClient.constructed.clear()
    _RedirectingClient.requested.clear()
    monkeypatch.setattr("sutr.auth_start.httpx.AsyncClient", _RedirectingClient)

    with pytest.raises(UnsafeUpstreamUrlError):
        await start_oauth_for_installed(session, _custom_installed(test_org), test_org)

    # The client was built so httpx cannot open the Location on its own.
    [kwargs] = _RedirectingClient.constructed
    assert kwargs.get("follow_redirects") is False
    # And nothing was ever sent to the address the redirect or the metadata named.
    assert not any(url.startswith(LOOPBACK_URL) for url in _RedirectingClient.requested)


class _LoopbackEndpointsClient(_RedirectingClient):
    """Metadata resolves fine, but the token endpoint points at loopback."""

    async def get(self, url, headers=None):
        type(self).requested.append(str(url))
        return httpx.Response(404, request=httpx.Request("GET", url, headers=headers))

    async def send(self, request):
        type(self).requested.append(str(request.url))
        if "oauth-protected-resource" in request.url.path:
            return httpx.Response(404, request=request)
        if "oauth-authorization-server" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "issuer": "https://mcp.example.com",
                    "authorization_endpoint": "https://mcp.example.com/authorize",
                    "token_endpoint": f"{LOOPBACK_URL}token",
                    "registration_endpoint": "https://mcp.example.com/register",
                },
                request=request,
            )
        raise AssertionError(f"unexpected request to {request.url}")


async def test_a_discovered_endpoint_on_the_local_network_is_refused(
    session, test_org, monkeypatch
):
    _LoopbackEndpointsClient.constructed.clear()
    _LoopbackEndpointsClient.requested.clear()
    monkeypatch.setattr("sutr.auth_start.httpx.AsyncClient", _LoopbackEndpointsClient)

    with pytest.raises(UnsafeUpstreamUrlError):
        await start_oauth_for_installed(session, _custom_installed(test_org), test_org)

    assert not any(url.startswith(LOOPBACK_URL) for url in _LoopbackEndpointsClient.requested)
