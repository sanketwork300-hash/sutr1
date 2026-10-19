"""SECURITY_BUGS.md S11 and S22: the SSRF screen pins the address it vetted,
and the tool hot path caps what it buffers.

A screen that resolves a name, approves it, and then lets the HTTP client
resolve it again is a screen with a gap exactly one DNS answer wide. These
tests hold the fix to its shape: one resolution, the request goes to the
address that passed, and the original hostname still rides in `Host` and in
TLS (SNI and certificate verification both read `sni_hostname`).
"""

import socket
from unittest.mock import patch

import httpx
import pytest

from sutr import api_client
from sutr import upstream_safety as u
from sutr.integrations.types import ApiTool
from sutr.models.integration import InstalledIntegration

PUBLIC = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _addrinfo(address: str):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]


# ── The pinned target ────────────────────────────────────────────────────────


def test_pinned_target_rewrites_the_host_and_keeps_the_name_in_host_and_sni(monkeypatch):
    monkeypatch.setattr(u, "_resolve_host", lambda hostname, port: (PUBLIC,))
    target = u.pinned_request_target("https://api.example.com/v1/things?x=1")
    assert target.url == f"https://{PUBLIC}/v1/things?x=1"
    assert target.hostname == "api.example.com"
    assert target.address == PUBLIC
    assert target.headers == {"Host": "api.example.com"}
    assert target.extensions == {"sni_hostname": "api.example.com"}


def test_pinned_target_keeps_an_explicit_port_in_url_and_host(monkeypatch):
    monkeypatch.setattr(u, "_resolve_host", lambda hostname, port: (PUBLIC,))
    target = u.pinned_request_target("http://api.example.com:8080/v1")
    assert target.url == f"http://{PUBLIC}:8080/v1"
    assert target.headers == {"Host": "api.example.com:8080"}


def test_pinned_target_brackets_an_ipv6_address(monkeypatch):
    monkeypatch.setattr(u, "_resolve_host", lambda hostname, port: (PUBLIC_V6,))
    target = u.pinned_request_target("https://api.example.com/v1")
    assert target.url == f"https://[{PUBLIC_V6}]/v1"
    assert target.headers == {"Host": "api.example.com"}


def test_pinned_target_refuses_a_name_with_any_blocked_answer(monkeypatch):
    """One public and one private answer is a rebinding setup, not a pass."""

    def mixed(hostname, port, **kwargs):
        return _addrinfo(PUBLIC) + _addrinfo("127.0.0.1")

    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    with pytest.raises(u.UnsafeUpstreamUrlError, match="blocked"):
        u.pinned_request_target("https://api.example.com/v1")


# ── The blocklist ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    [
        "100.100.100.200",  # CGNAT, where some clouds keep their metadata service
        "100.64.0.1",
        "64:ff9b::7f00:1",  # NAT64 well-known prefix wrapping 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 wrapping 169.254.169.254
        "64:ff9b:1::1",  # NAT64 local-use prefix
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",  # IPv4-mapped link-local
        "::127.0.0.1",  # IPv4-compatible (deprecated) loopback
        "198.18.0.1",  # benchmarking range, not global
        "192.0.0.8",
        "0.0.0.0",
        "::",
        "fe80::1",
        "fc00::1",
    ],
)
def test_blocked_ranges_are_blocked(address):
    assert u._blocked_ip(address) is True


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "::ffff:8.8.8.8", "2001:4860:4860::8888"])
def test_public_addresses_are_allowed(address):
    assert u._blocked_ip(address) is False


# ── The dispatch path: one resolution, pinned connection ────────────────────


def _tool() -> ApiTool:
    return ApiTool(name="get_things", description="d", method="GET", path="/things")


def _capturing_client(sent: list[httpx.Request], body: bytes = b'{"ok": true}'):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return Patched


async def test_dispatch_connects_to_the_vetted_address_and_never_resolves_again(monkeypatch):
    """A rebinding name answers public once and loopback thereafter. The
    first answer is the one the connection must use, and the second must
    never be asked for."""
    answers = iter([_addrinfo(PUBLIC), _addrinfo("127.0.0.1")])
    calls: list[str] = []

    def rebinding(hostname, port, **kwargs):
        calls.append(hostname)
        return next(answers)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    sent: list[httpx.Request] = []
    monkeypatch.setattr("sutr.api_client.httpx.AsyncClient", _capturing_client(sent))

    result = await api_client.dispatch_api_tool(
        base_url="https://api.example.com", tool_def=_tool(), args={}, headers={}
    )

    assert result["isError"] is False
    assert calls == ["api.example.com"], "the name was resolved more than once"
    [request] = sent
    assert request.url.host == PUBLIC
    assert request.url.path == "/things"
    assert request.headers["host"] == "api.example.com"
    assert request.extensions["sni_hostname"] == "api.example.com"


async def test_dispatch_refuses_a_name_that_resolves_to_a_blocked_address(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda hostname, port, **kw: _addrinfo("169.254.169.254")
    )
    with patch("httpx.AsyncClient.stream") as stream:
        result = await api_client.dispatch_api_tool(
            base_url="https://api.example.com", tool_def=_tool(), args={}, headers={}
        )
        stream.assert_not_called()
    assert result["isError"] is True
    assert "Unsafe upstream URL" in result["content"][0]["text"]


# ── S22: the hot path is capped ──────────────────────────────────────────────


def _installed(test_org) -> InstalledIntegration:
    return InstalledIntegration(
        org_id=test_org.id,
        integration_id="customapi_big",
        type="custom",
        url="https://api.example.com",
        auth_method="none",
        connected=True,
    )


async def test_call_tool_truncates_at_the_configured_cap(test_org, monkeypatch):
    monkeypatch.setattr(u, "_resolve_host", lambda hostname, port: (PUBLIC,))
    monkeypatch.setattr("sutr.api_client.settings.tool_response_max_bytes", 1024)
    sent: list[httpx.Request] = []
    monkeypatch.setattr(
        "sutr.api_client.httpx.AsyncClient", _capturing_client(sent, body=b"x" * 10_000)
    )

    result = await api_client.call_tool(_installed(test_org), _tool(), {}, auth_headers={})

    assert result["isError"] is True
    assert result["truncated"] is True
    assert "truncated" in result["content"][0]["text"]
    assert "x" * 100 not in result["content"][0]["text"]


async def test_call_tool_passes_an_explicit_cap_through(test_org, monkeypatch):
    monkeypatch.setattr(u, "_resolve_host", lambda hostname, port: (PUBLIC,))
    sent: list[httpx.Request] = []
    monkeypatch.setattr(
        "sutr.api_client.httpx.AsyncClient", _capturing_client(sent, body=b"y" * 50)
    )

    result = await api_client.call_tool(
        _installed(test_org), _tool(), {}, auth_headers={}, max_response_bytes=10
    )
    assert result["truncated"] is True

    result = await api_client.call_tool(
        _installed(test_org), _tool(), {}, auth_headers={}, max_response_bytes=100
    )
    assert result["isError"] is False
    assert "truncated" not in result


async def test_call_tool_caps_the_request_body_by_default(test_org, monkeypatch):
    monkeypatch.setattr(u, "_resolve_host", lambda hostname, port: (PUBLIC,))
    sent: list[httpx.Request] = []
    monkeypatch.setattr("sutr.api_client.httpx.AsyncClient", _capturing_client(sent))
    from sutr.integrations.types import Param

    tool = ApiTool(
        name="post_blob",
        description="d",
        method="POST",
        path="/blobs",
        params=[Param(name="blob", type="string", required=True, location="body")],
    )
    huge = "z" * (api_client.DEFAULT_MAX_REQUEST_BODY_BYTES + 1)
    result = await api_client.call_tool(_installed(test_org), tool, {"blob": huge}, auth_headers={})
    assert result["isError"] is True
    assert "too large" in result["content"][0]["text"]
    assert sent == []


def test_the_pipeline_passes_the_cap_explicitly():
    """The hot path must name the cap, not inherit it: a reader of the
    pipeline should see the promise made to the replica's other tenants."""
    import inspect

    from sutr.services import tool_pipeline

    source = inspect.getsource(tool_pipeline.execute_tool)
    assert "max_response_bytes=settings.tool_response_max_bytes" in source
