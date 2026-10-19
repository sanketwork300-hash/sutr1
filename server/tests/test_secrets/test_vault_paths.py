"""Vault paths are built from refs that carry user input; each segment is encoded."""

import httpx
import pytest

from sutr.config import settings
from sutr.models.secret import Secret
from sutr.secrets import vault


@pytest.fixture(name="server")
def server_fixture(monkeypatch):
    monkeypatch.setattr(settings, "vault_addr", "https://vault.example:8200")
    monkeypatch.setattr(settings, "vault_token", "a-token")
    monkeypatch.setattr(settings, "vault_mount", "secret")
    monkeypatch.setattr(settings, "vault_path_prefix", "sutr")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": {"data": {"value": "v"}}})

    monkeypatch.setattr(
        vault,
        "make_client",
        lambda: httpx.Client(base_url=settings.vault_addr, transport=httpx.MockTransport(handler)),
    )
    return calls


def test_a_plain_ref_is_unchanged(server):
    vault.VaultSecretsBackend().store("credentials/org/customapi_x/apiKey", "v")
    assert server[0].url.raw_path == b"/v1/secret/data/sutr/credentials/org/customapi_x/apiKey"


def test_a_slash_in_a_segment_cannot_add_a_path_segment(server):
    backend = vault.VaultSecretsBackend()
    assert backend._path("credentials/org/int/a%2Fb") == "sutr/credentials/org/int/a%252Fb"
    # Round trip: the same ref stores and retrieves at the same place.
    backend.store("credentials/org/int/a%2Fb", "v")
    backend.retrieve(Secret(kind="k", storage_backend="vault", ref="credentials/org/int/a%2Fb"))
    assert server[0].url.raw_path == server[1].url.raw_path
    assert server[0].url.raw_path.endswith(b"/int/a%252Fb")


def test_dot_segments_cannot_walk_out_of_the_prefix(server):
    backend = vault.VaultSecretsBackend()
    assert backend._path("../../sys/policies") == "sutr/%2E%2E/%2E%2E/sys/policies"
    backend.retrieve(Secret(kind="k", storage_backend="vault", ref="../../sys/policies"))
    assert server[0].url.raw_path.startswith(b"/v1/secret/data/sutr/")
    assert b"/sys/policies" in server[0].url.raw_path
    assert b"/v1/secret/data/sys" not in server[0].url.raw_path


def test_query_and_fragment_characters_are_encoded(server):
    backend = vault.VaultSecretsBackend()
    assert backend._path("a?x=1#frag") == "sutr/a%3Fx%3D1%23frag"
    backend.store("a?x=1#frag", "v")
    assert server[0].url.raw_path == b"/v1/secret/data/sutr/a%3Fx%3D1%23frag"
    assert server[0].url.query == b""


def test_empty_segments_are_dropped(server):
    assert vault.VaultSecretsBackend()._path("//a///b/") == "sutr/a/b"
