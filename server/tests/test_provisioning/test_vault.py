"""The Vault KV v2 secrets backend, against a stub server.

**NOT TESTED against a live Vault.** These assert the requests Sutr sends —
paths, methods and bodies — against the documented KV v2 API. That is a design
review, not a deployment, and the traceability matrix says so.
"""

import json

import httpx
import pytest

from sutr.config import settings
from sutr.models.secret import Secret
from sutr.secrets import vault


class Recorder:
    def __init__(self, responses=None):
        self.calls: list[httpx.Request] = []
        self.responses = responses or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, request.url.path)
        if key in self.responses:
            status, payload = self.responses[key]
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json={})

    def paths(self, method: str) -> list[str]:
        return [call.url.path for call in self.calls if call.method == method]


@pytest.fixture(name="server")
def server_fixture(monkeypatch):
    monkeypatch.setattr(settings, "vault_addr", "https://vault.example:8200")
    monkeypatch.setattr(settings, "vault_token", "a-token")
    monkeypatch.setattr(settings, "vault_mount", "secret")
    monkeypatch.setattr(settings, "vault_path_prefix", "sutr")
    recorder = Recorder()
    monkeypatch.setattr(
        vault,
        "make_client",
        lambda: httpx.Client(
            base_url=settings.vault_addr, transport=httpx.MockTransport(recorder.handler)
        ),
    )
    return recorder


def _secret(ref: str = "orgs/abc/token") -> Secret:
    return Secret(ref=ref, kind="integration_token", storage_backend="vault", org_id=None)


def test_storing_writes_to_the_kv_v2_data_path(server):
    backend = vault.VaultSecretsBackend()
    stored = backend.store("orgs/abc/token", "sk-live-123")

    call = server.calls[0]
    assert call.method == "POST"
    assert call.url.path == "/v1/secret/data/sutr/orgs/abc/token"
    assert json.loads(call.content) == {"data": {"value": "sk-live-123"}}
    # Nothing sensitive comes back: the row records where the secret is.
    assert stored.value is None
    assert stored.encrypted_data_key is None


def test_retrieving_unwraps_the_kv_v2_envelope(server):
    server.responses[("GET", "/v1/secret/data/sutr/orgs/abc/token")] = (
        200,
        {"data": {"data": {"value": "sk-live-123"}, "metadata": {"version": 3}}},
    )
    assert vault.VaultSecretsBackend().retrieve(_secret()) == "sk-live-123"


def test_the_client_carries_the_vault_token(monkeypatch):
    """Asserted against the real client, not the stub the other tests inject."""
    monkeypatch.setattr(settings, "vault_addr", "https://vault.example:8200")
    monkeypatch.setattr(settings, "vault_token", "a-token")
    with vault.make_client() as client:
        assert client.headers["x-vault-token"] == "a-token"
        assert str(client.base_url) == "https://vault.example:8200"


def test_a_missing_secret_is_none_rather_than_an_error(server):
    server.responses[("GET", "/v1/secret/data/sutr/orgs/abc/token")] = (404, {"errors": []})
    assert vault.VaultSecretsBackend().retrieve(_secret()) is None


def test_deleting_removes_every_version(server):
    """The data endpoint leaves prior versions readable, which is not a delete."""
    vault.VaultSecretsBackend().delete(_secret())
    assert server.paths("DELETE") == ["/v1/secret/metadata/sutr/orgs/abc/token"]


def test_a_refused_token_explains_the_policy_rather_than_the_status(server):
    server.responses[("GET", "/v1/secret/data/sutr/orgs/abc/token")] = (
        403,
        {"errors": ["permission denied"]},
    )
    with pytest.raises(vault.VaultError, match="may lack a policy"):
        vault.VaultSecretsBackend().retrieve(_secret())


def test_an_unreachable_vault_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(settings, "vault_addr", "https://vault.example:8200")
    monkeypatch.setattr(settings, "vault_token", "a-token")

    def failing():
        def handler(request):
            raise httpx.ConnectError("no route to host")

        return httpx.Client(base_url=settings.vault_addr, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(vault, "make_client", failing)
    with pytest.raises(vault.VaultError, match="Could not reach Vault"):
        vault.VaultSecretsBackend().retrieve(_secret())


def test_an_unconfigured_vault_refuses_to_construct(monkeypatch):
    monkeypatch.setattr(settings, "vault_addr", "")
    monkeypatch.setattr(settings, "vault_token", "")
    with pytest.raises(RuntimeError, match="NOT_CONFIGURED"):
        vault.VaultSecretsBackend()


def test_the_description_does_not_imply_dynamic_credentials(monkeypatch):
    """Storing a secret is not minting one, and the difference is stated."""
    monkeypatch.setattr(settings, "secrets_backend", "vault")
    monkeypatch.setattr(settings, "vault_addr", "https://vault.example:8200")
    monkeypatch.setattr(settings, "vault_token", "a-token")
    described = vault.describe()
    assert described["at_rest"] == "Vault (KV v2)"
    assert described["vault"]["configured"] is True
    assert described["vault"]["tested_against_a_live_server"] is False
    assert described["dynamic_credentials"]["available"] is False
    assert "NOT IMPLEMENTED" in described["dynamic_credentials"]["unavailable_reason"]


def test_the_default_install_says_secrets_are_plaintext(monkeypatch):
    """§4.3.6 is a deliberate deviation, and it is recorded rather than hidden."""
    monkeypatch.setattr(settings, "secrets_backend", "db")
    described = vault.describe()
    assert described["at_rest"] == "plaintext in the database"
    assert described["credentials_in_generated_code"] is False
    assert described["credentials_injected_as_env_vars"] is True
