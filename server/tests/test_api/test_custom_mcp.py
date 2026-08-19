"""Custom remote-MCP integration tests: SSRF screening and RBAC (Phase 3).

Previously this router had no tests at all — and no URL screening, meaning a
user-supplied MCP URL could point the server at cloud metadata or internal
services.
"""

import pytest

from sutr.mcp.client import _ensure_safe_upstream
from sutr.models.integration import InstalledIntegration
from sutr.models.org_membership import OrgMembership
from sutr.upstream_safety import UnsafeUpstreamUrlError


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Deterministic DNS: any non-literal hostname resolves to a public IP.
    IP-literal URLs (127.0.0.1, 169.254.169.254) bypass this and are judged
    directly by the blocklist."""
    import socket as socket_module

    real_getaddrinfo = socket_module.getaddrinfo

    def fake_resolve(hostname: str, port: int):
        try:
            real_getaddrinfo(hostname, port)  # keep literals working
        except OSError:
            pass
        import ipaddress

        try:
            ipaddress.ip_address(hostname)
            return None  # literal — let the real resolver handle it
        except ValueError:
            return ("93.184.216.34",)

    from sutr import upstream_safety

    real = upstream_safety._resolve_host

    def patched(hostname, port):
        resolved = fake_resolve(hostname, port)
        if resolved is None:
            return real(hostname, port)
        blocked = [ip for ip in resolved if upstream_safety._blocked_ip(ip)]
        if blocked:
            raise UnsafeUpstreamUrlError("Host resolves to a blocked network range")
        return resolved

    monkeypatch.setattr(upstream_safety, "_resolve_host", patched)


async def test_create_custom_mcp_ok(client):
    resp = await client.post(
        "/api/integrations/custom",
        json={"name": "My MCP", "url": "https://mcp.example.com/mcp", "auth_method": "none"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["integration_id"].startswith("custom_")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/mcp",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/mcp",
        "http://[::1]/mcp",
    ],
)
async def test_create_custom_mcp_rejects_internal_urls(client, url):
    resp = await client.post(
        "/api/integrations/custom",
        json={"name": "Sneaky", "url": url, "auth_method": "none"},
    )
    assert resp.status_code == 400
    assert "Unsafe MCP server URL" in resp.json()["detail"]


async def test_update_custom_mcp_rejects_internal_url(client):
    created = await client.post(
        "/api/integrations/custom",
        json={"name": "Mine", "url": "https://mcp.example.com/mcp", "auth_method": "none"},
    )
    row_id = created.json()["id"]
    resp = await client.patch(
        f"/api/integrations/custom/{row_id}",
        json={"url": "http://127.0.0.1:9000/mcp"},
    )
    assert resp.status_code == 400


async def test_viewer_cannot_create_custom_mcp(client, session, test_user, test_org):
    membership = session.get(OrgMembership, (test_user.id, test_org.id))
    membership.role = "viewer"
    session.add(membership)
    session.commit()

    resp = await client.post(
        "/api/integrations/custom",
        json={"name": "Nope", "url": "https://mcp.example.com/mcp", "auth_method": "none"},
    )
    assert resp.status_code == 403


def test_mcp_client_guard_blocks_internal_custom_url(test_org):
    installed = InstalledIntegration(
        org_id=test_org.id,
        integration_id="custom_evil",
        type="remote_mcp",
        url="http://169.254.169.254/latest/meta-data/",
        auth_method="none",
        connected=True,
    )
    with pytest.raises(UnsafeUpstreamUrlError):
        _ensure_safe_upstream(installed)


def test_mcp_client_guard_skips_bundled(test_org):
    installed = InstalledIntegration(
        org_id=test_org.id,
        integration_id="github",
        type="remote_mcp",
        url="https://api.githubcopilot.com/mcp/",
        auth_method="token",
        connected=True,
    )
    _ensure_safe_upstream(installed)  # bundled — no validation, must not raise
