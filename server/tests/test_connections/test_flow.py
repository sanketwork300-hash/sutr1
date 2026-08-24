"""The authorization-code grant: URL building, token parsing, expiry, identity.

The parsing tests exist because GitHub and Google disagree about the content
type of a token response, and a silent parse failure there is indistinguishable
from a user declining the consent screen.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from sutr.connections.errors import ConnectError
from sutr.connections.flow import (
    authorization_url,
    exchange_code,
    expires_at,
    fetch_account_label,
    pkce_pair,
    refresh_access_token,
)
from sutr.connections.providers import get_provider


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_id", "gh-client")
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_secret", "gh-secret")
    monkeypatch.setattr("sutr.config.settings.gcp_oauth_client_id", "gcp-client")
    monkeypatch.setattr("sutr.config.settings.gcp_oauth_client_secret", "gcp-secret")
    monkeypatch.setattr("sutr.config.settings.base_url", "https://sutr.example.com")


def _mock_transport(monkeypatch, handler):
    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.connections.flow.httpx.AsyncClient", Patched)


# ── authorization URL ────────────────────────────────────────────────────────


def test_authorization_url_carries_pkce_and_the_registered_callback():
    provider = get_provider("github")
    url = authorization_url(provider, state="st4te", code_challenge="chall3nge")
    query = parse_qs(urlsplit(url).query)
    assert query["client_id"] == ["gh-client"]
    assert query["state"] == ["st4te"]
    assert query["code_challenge"] == ["chall3nge"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://sutr.example.com/api/connections/github/callback"]


def test_google_asks_for_offline_access():
    """Without it Google never issues a refresh token, and a deployment that
    cannot be reconciled tomorrow is not a deployment."""
    query = parse_qs(
        urlsplit(authorization_url(get_provider("gcp"), state="s", code_challenge="c")).query
    )
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]


def test_pkce_verifier_and_challenge_differ_each_call():
    first, _ = pkce_pair()
    second, _ = pkce_pair()
    assert first != second
    assert len(first) == 96


# ── token responses ──────────────────────────────────────────────────────────


async def test_github_form_encoded_token_response_is_understood(monkeypatch):
    """GitHub answers in x-www-form-urlencoded unless coaxed otherwise."""

    def handler(request):
        return httpx.Response(
            200,
            text="access_token=gho_abc&scope=repo&token_type=bearer",
            headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8"},
        )

    _mock_transport(monkeypatch, handler)
    payload = await exchange_code(get_provider("github"), code="c", code_verifier="v")
    assert payload["access_token"] == "gho_abc"
    assert payload["scope"] == "repo"


async def test_json_token_response_is_understood(monkeypatch):
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"access_token": "ya29", "expires_in": 3599, "refresh_token": "1//r"}
        ),
    )
    payload = await exchange_code(get_provider("gcp"), code="c", code_verifier="v")
    assert payload["refresh_token"] == "1//r"


async def test_provider_error_description_reaches_the_user(monkeypatch):
    _mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "error": "bad_verification_code",
                "error_description": "The code passed is incorrect or expired.",
            },
        ),
    )
    with pytest.raises(ConnectError) as excinfo:
        await exchange_code(get_provider("github"), code="c", code_verifier="v")
    assert excinfo.value.code == "token_exchange_failed"
    assert "incorrect or expired" in excinfo.value.message


async def test_a_response_without_a_token_is_a_failure(monkeypatch):
    _mock_transport(monkeypatch, lambda request: httpx.Response(200, json={"token_type": "bearer"}))
    with pytest.raises(ConnectError):
        await exchange_code(get_provider("gcp"), code="c", code_verifier="v")


async def test_refresh_sends_the_refresh_grant_and_scopes(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3599})

    _mock_transport(monkeypatch, handler)
    payload = await refresh_access_token(get_provider("gcp"), "1//refresh")
    assert seen["grant_type"] == ["refresh_token"]
    assert seen["refresh_token"] == ["1//refresh"]
    assert payload["access_token"] == "fresh"


# ── expiry ───────────────────────────────────────────────────────────────────


def test_missing_expires_in_means_no_expiry():
    """GitHub OAuth-app tokens never expire and omit the field. Treating that
    as 'expired now' would refresh on every single call."""
    assert expires_at({"access_token": "gho"}) is None


def test_expires_in_becomes_an_absolute_instant():
    resolved = expires_at({"expires_in": 3600})
    assert resolved is not None
    assert timedelta(minutes=58) < resolved - datetime.now(timezone.utc) < timedelta(minutes=61)


def test_unparseable_expires_in_is_treated_as_no_expiry():
    assert expires_at({"expires_in": "soon"}) is None


# ── identity ─────────────────────────────────────────────────────────────────


async def test_github_label_comes_from_the_user_endpoint(monkeypatch):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer gho_abc"
        return httpx.Response(200, json={"login": "kumar-abhinav"})

    _mock_transport(monkeypatch, handler)
    label = await fetch_account_label(get_provider("github"), {"access_token": "gho_abc"})
    assert label == "kumar-abhinav"


async def test_a_failed_identity_lookup_does_not_fail_the_connection(monkeypatch):
    """The grant is what matters; the label is decoration."""
    _mock_transport(monkeypatch, lambda request: httpx.Response(500))
    assert await fetch_account_label(get_provider("github"), {"access_token": "x"}) == ""


async def test_cloud_label_is_read_from_the_id_token_claims():
    import base64
    import json

    claims = base64.urlsafe_b64encode(json.dumps({"email": "dev@example.com"}).encode())
    id_token = "header." + claims.decode().rstrip("=") + ".signature"
    label = await fetch_account_label(
        get_provider("gcp"), {"access_token": "ya29", "id_token": id_token}
    )
    assert label == "dev@example.com"


async def test_a_malformed_id_token_yields_no_label():
    assert (
        await fetch_account_label(get_provider("gcp"), {"access_token": "y", "id_token": "junk"})
        == ""
    )
