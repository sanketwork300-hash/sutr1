"""Per-scheme credentials end to end (build prompt §24, ADR-009).

Storage, the configuration API, the OAuth2 client-credentials grant the
platform runs itself, and — the point of all of it — the credential arriving
in the right place on the outbound request.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from sutr.credentials.resolve import client_credentials_token, resolve_for_integration
from sutr.credentials.store import (
    delete_credential,
    describe,
    encode_basic,
    get_credential,
    list_credentials,
    upsert_credential,
)
from sutr.models.custom_api_integration import CustomApiIntegration
from sutr.models.integration_credential import (
    KIND_BASIC,
    KIND_CLIENT_CREDENTIALS,
    KIND_SECRET,
)
from sutr.openapi.normalizer import normalize
from sutr.openapi.security import (
    AuthTranslation,
    CredentialPlacement,
    OAuthFlow,
    translate_security,
)
from sutr.secrets.records import get_secret_value

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Secured", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com"}],
    "components": {
        "securitySchemes": {
            "hdr": {"type": "apiKey", "in": "header", "name": "X-Api-Key"},
            "qry": {"type": "apiKey", "in": "query", "name": "api_key"},
            "basic": {"type": "http", "scheme": "basic"},
            "oauth": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.com/token",
                        "scopes": {"read": "read"},
                    }
                },
            },
        }
    },
    "security": [{"hdr": []}],
    "paths": {
        "/things": {
            "get": {
                "operationId": "listThings",
                "summary": "List things.",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}

INTEGRATION_ID = "customapi_secured"


@pytest.fixture(autouse=True)
def allow_example_hosts(monkeypatch):
    """`auth.example.com` does not resolve, and the SSRF screen rightly refuses
    an unresolvable host. The screen itself is tested in `test_security/`."""

    def _permit(url: str, allow_query: bool = False) -> None:
        return None

    monkeypatch.setattr("sutr.credentials.resolve.validate_safe_url", _permit)


@pytest.fixture(name="auth")
def auth_fixture() -> AuthTranslation:
    return translate_security(normalize(SPEC))


@pytest.fixture(name="integration")
def integration_fixture(session, test_org, auth) -> CustomApiIntegration:
    integration = CustomApiIntegration(
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        name="Secured",
        base_url="https://api.example.com",
        token_header=auth.token_header,
        token_format=auth.token_format,
        tools_json="[]",
        auth_json=auth.model_dump_json(),
    )
    session.add(integration)
    session.commit()
    return integration


def _placement(auth: AuthTranslation, name: str) -> CredentialPlacement:
    return next(p for p in auth.placements if p.scheme_name == name)


# ── Storage ──────────────────────────────────────────────────────────────────


def test_a_stored_credential_keeps_only_a_secret_reference(session, test_org, auth):
    credential = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "hdr"),
        value="sk_live_secret",
    )
    session.commit()
    assert credential.kind == KIND_SECRET
    assert credential.secret_id is not None
    # The value is not on the credential row itself.
    assert "sk_live_secret" not in json.dumps(describe(credential))
    assert get_secret_value(session, credential.secret_id) == "sk_live_secret"


def test_updating_a_credential_replaces_the_value_in_place(session, test_org, auth):
    placement = _placement(auth, "hdr")
    first = upsert_credential(
        session, org_id=test_org.id, integration_id=INTEGRATION_ID, placement=placement, value="a"
    )
    session.commit()
    second = upsert_credential(
        session, org_id=test_org.id, integration_id=INTEGRATION_ID, placement=placement, value="b"
    )
    session.commit()
    assert first.id == second.id
    assert get_secret_value(session, second.secret_id) == "b"
    assert len(list_credentials(session, test_org.id, INTEGRATION_ID)) == 1


def test_basic_credentials_are_encoded_once_on_the_way_in(session, test_org, auth):
    credential = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "basic"),
        value=encode_basic("ada", "hunter2"),
    )
    session.commit()
    assert credential.kind == KIND_BASIC
    assert get_secret_value(session, credential.secret_id) == "YWRhOmh1bnRlcjI="


def test_an_unusable_scheme_cannot_hold_a_credential(session, test_org):
    unusable = CredentialPlacement(
        scheme_name="mtls", scheme_type="mutualTLS", requires="unsupported"
    )
    with pytest.raises(ValueError, match="cannot hold a credential"):
        upsert_credential(
            session,
            org_id=test_org.id,
            integration_id=INTEGRATION_ID,
            placement=unusable,
            value="x",
        )


def test_deleting_a_credential_removes_its_secret(session, test_org, auth):
    credential = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "hdr"),
        value="gone",
    )
    session.commit()
    secret_id = credential.secret_id
    assert delete_credential(session, test_org.id, INTEGRATION_ID, "hdr") is True
    session.commit()
    assert get_secret_value(session, secret_id) is None
    assert get_credential(session, test_org.id, INTEGRATION_ID, "hdr") is None


# ── Resolution ───────────────────────────────────────────────────────────────


async def test_resolution_returns_each_credential_with_its_placement(session, test_org, auth):
    upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "hdr"),
        value="hk",
    )
    upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "qry"),
        value="qk",
    )
    session.commit()

    resolved = await resolve_for_integration(session, test_org.id, INTEGRATION_ID)
    by_name = {c["name"]: c for c in resolved}
    assert by_name["X-Api-Key"] == {
        "location": "header",
        "name": "X-Api-Key",
        "format": "{token}",
        "value": "hk",
    }
    assert by_name["api_key"]["location"] == "query"


async def test_an_unconfigured_credential_is_omitted_not_sent_empty(session, test_org, auth):
    upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "hdr"),
        value=None,
    )
    session.commit()
    assert await resolve_for_integration(session, test_org.id, INTEGRATION_ID) == []


# ── OAuth2 client credentials, run by the platform ───────────────────────────


def _token_response(token="at-1", expires_in=3600, status=200):
    return httpx.Response(
        status,
        json={"access_token": token, "token_type": "Bearer", "expires_in": expires_in},
        request=httpx.Request("POST", "https://auth.example.com/token"),
    )


async def test_client_credentials_grant_is_executed_and_cached(session, test_org, auth):
    credential = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "oauth"),
        client_id="cid",
        client_secret="csecret",
    )
    session.commit()
    assert credential.kind == KIND_CLIENT_CREDENTIALS
    assert credential.token_url == "https://auth.example.com/token"
    assert credential.scopes == "read"

    post = AsyncMock(return_value=_token_response())
    with patch("httpx.AsyncClient.post", post):
        token = await client_credentials_token(session, credential)
    assert token == "at-1"
    assert post.await_count == 1
    call = post.await_args
    assert call.args[0] == "https://auth.example.com/token"
    assert call.kwargs["data"]["grant_type"] == "client_credentials"
    assert call.kwargs["data"]["scope"] == "read"
    assert call.kwargs["auth"] == ("cid", "csecret")

    # Second call is served from the cache — no second token request.
    with patch("httpx.AsyncClient.post", post):
        again = await client_credentials_token(session, credential)
    assert again == "at-1"
    assert post.await_count == 1


async def test_an_expired_token_is_refetched(session, test_org, auth):
    credential = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "oauth"),
        client_id="cid",
        client_secret="csecret",
    )
    session.commit()
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=_token_response("first"))):
        assert await client_credentials_token(session, credential) == "first"

    credential.expires_at = datetime.utcnow() - timedelta(seconds=1)
    session.add(credential)
    session.commit()

    with patch("httpx.AsyncClient.post", AsyncMock(return_value=_token_response("second"))):
        assert await client_credentials_token(session, credential) == "second"


async def test_a_failing_token_endpoint_yields_no_credential_rather_than_an_exception(
    session, test_org, auth
):
    credential = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=_placement(auth, "oauth"),
        client_id="cid",
        client_secret="csecret",
    )
    session.commit()
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=_token_response(status=401))):
        assert await client_credentials_token(session, credential) is None
    # The whole integration still resolves; one broken scheme is not fatal.
    assert await resolve_for_integration(session, test_org.id, INTEGRATION_ID) == []


async def test_a_token_endpoint_pointing_at_a_private_address_is_refused(
    session, test_org, auth, monkeypatch
):
    # This one test wants the real screen back.
    from sutr.upstream_safety import validate_safe_url as real_validate

    monkeypatch.setattr("sutr.credentials.resolve.validate_safe_url", real_validate)
    placement = _placement(auth, "oauth").model_copy(deep=True)
    placement.flow = OAuthFlow(kind="clientCredentials", token_url="http://169.254.169.254/token")
    credential = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=placement,
        client_id="cid",
        client_secret="csecret",
    )
    session.commit()
    post = AsyncMock(return_value=_token_response())
    with patch("httpx.AsyncClient.post", post):
        assert await client_credentials_token(session, credential) is None
    assert post.await_count == 0, "the token endpoint must not be contacted at all"


async def test_changing_the_client_secret_invalidates_the_cached_token(session, test_org, auth):
    placement = _placement(auth, "oauth")
    credential = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=placement,
        client_id="cid",
        client_secret="old",
    )
    session.commit()
    with patch("httpx.AsyncClient.post", AsyncMock(return_value=_token_response("stale"))):
        await client_credentials_token(session, credential)
    assert credential.access_token_secret_id is not None

    updated = upsert_credential(
        session,
        org_id=test_org.id,
        integration_id=INTEGRATION_ID,
        placement=placement,
        client_id="cid",
        client_secret="new",
    )
    session.commit()
    assert updated.access_token_secret_id is None
    assert updated.expires_at is None


# ── The HTTP API ─────────────────────────────────────────────────────────────


async def test_listing_reports_every_declared_scheme_and_what_it_needs(client, integration):
    resp = await client.get(f"/api/integrations/{INTEGRATION_ID}/credentials")
    assert resp.status_code == 200
    body = resp.json()
    schemes = {s["scheme_name"]: s for s in body["schemes"]}
    assert set(schemes) == {"hdr", "qry", "basic", "oauth"}
    assert schemes["qry"]["location"] == "query"
    assert schemes["oauth"]["requires"] == "client_credentials"
    assert schemes["oauth"]["flow"]["token_url"] == "https://auth.example.com/token"
    assert all(s["credential"] is None for s in schemes.values())


async def test_setting_and_removing_a_credential(client, integration, session, test_org):
    resp = await client.put(
        f"/api/integrations/{INTEGRATION_ID}/credentials",
        json={"scheme_name": "hdr", "value": "sk_live_x"},
    )
    assert resp.status_code == 200
    assert resp.json()["configured"] is True
    assert "sk_live_x" not in resp.text

    listing = await client.get(f"/api/integrations/{INTEGRATION_ID}/credentials")
    hdr = next(s for s in listing.json()["schemes"] if s["scheme_name"] == "hdr")
    assert hdr["credential"]["configured"] is True

    removed = await client.delete(f"/api/integrations/{INTEGRATION_ID}/credentials/hdr")
    assert removed.status_code == 204
    listing = await client.get(f"/api/integrations/{INTEGRATION_ID}/credentials")
    hdr = next(s for s in listing.json()["schemes"] if s["scheme_name"] == "hdr")
    assert hdr["credential"] is None


async def test_basic_credentials_are_accepted_as_a_username_and_password(client, integration):
    resp = await client.put(
        f"/api/integrations/{INTEGRATION_ID}/credentials",
        json={"scheme_name": "basic", "username": "ada", "password": "hunter2"},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "basic"


async def test_a_client_credentials_scheme_requires_client_id_and_secret(client, integration):
    resp = await client.put(
        f"/api/integrations/{INTEGRATION_ID}/credentials",
        json={"scheme_name": "oauth", "value": "just-a-token"},
    )
    assert resp.status_code == 400
    assert "client_id" in resp.json()["detail"]

    ok = await client.put(
        f"/api/integrations/{INTEGRATION_ID}/credentials",
        json={"scheme_name": "oauth", "client_id": "cid", "client_secret": "csecret"},
    )
    assert ok.status_code == 200
    assert ok.json()["client_id"] == "cid"
    assert "csecret" not in ok.text


async def test_an_undeclared_scheme_is_rejected(client, integration):
    resp = await client.put(
        f"/api/integrations/{INTEGRATION_ID}/credentials",
        json={"scheme_name": "invented", "value": "x"},
    )
    assert resp.status_code == 404


async def test_setting_a_credential_is_audited(client, integration, session, test_org):
    from sqlmodel import select

    from sutr.models.audit_event import AuditEvent

    await client.put(
        f"/api/integrations/{INTEGRATION_ID}/credentials",
        json={"scheme_name": "hdr", "value": "sk_live_x"},
    )
    events = session.exec(
        select(AuditEvent).where(AuditEvent.action == "integration.credential.set")
    ).all()
    assert len(events) == 1
    assert "sk_live_x" not in json.dumps(
        {"summary": events[0].summary, "metadata": events[0].metadata_json}
    )
