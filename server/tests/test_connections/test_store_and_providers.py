"""Connection persistence and provider configuration.

The behaviour under test that is easiest to get wrong: re-authorizing with a
provider that does not resend a refresh token must not silently turn a
renewable connection into a single-use one.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sutr.connections.errors import ConnectError
from sutr.connections.providers import callback_url, get_provider, is_configured
from sutr.connections.store import (
    access_token,
    delete_connection,
    find_connection,
    list_connections,
    metadata_of,
    save_connection,
    serialize,
)
from sutr.models.secret import Secret
from sutr.secrets.records import get_secret_value


@pytest.fixture(autouse=True)
def _base_url(monkeypatch):
    monkeypatch.setattr("sutr.config.settings.base_url", "https://sutr.example.com")


def _save(session, org, user, **overrides):
    kwargs = {
        "org_id": org.id,
        "user_id": user.id,
        "provider": "github",
        "access_token_value": "gho_first",
        "refresh_token_value": "refresh_first",
        "scopes": "repo",
        "account_label": "octocat",
        "expires": None,
        "metadata": {},
    }
    kwargs.update(overrides)
    connection = save_connection(session, **kwargs)
    session.commit()
    return connection


# ── provider configuration ───────────────────────────────────────────────────


def test_unconfigured_provider_names_the_env_vars_and_the_callback(monkeypatch):
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_id", "")
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_secret", "")
    configured, reason = is_configured("github")
    assert configured is False
    assert "GITHUB_OAUTH_CLIENT_ID" in reason
    assert "https://sutr.example.com/api/connections/github/callback" in reason


def test_aws_is_unconfigured_without_a_start_url(monkeypatch):
    monkeypatch.setattr("sutr.config.settings.aws_sso_start_url", "")
    configured, reason = is_configured("aws")
    assert configured is False
    assert "AWS_SSO_START_URL" in reason


def test_aws_needs_no_client_secret(monkeypatch):
    """The device grant registers its client dynamically, so there is nothing
    for an operator to paste beyond the portal URL."""
    monkeypatch.setattr("sutr.config.settings.aws_sso_start_url", "https://d-1.awsapps.com/start")
    assert is_configured("aws") == (True, None)


def test_unknown_provider_is_rejected():
    assert get_provider("gitlab") is None
    assert is_configured("gitlab")[0] is False


def test_callback_url_is_stable_per_provider():
    assert callback_url("gcp") == "https://sutr.example.com/api/connections/gcp/callback"


# ── persistence ──────────────────────────────────────────────────────────────


def test_tokens_are_stored_as_secrets_not_columns(session, test_org, test_user):
    connection = _save(session, test_org, test_user)
    assert connection.access_secret_id is not None
    assert get_secret_value(session, connection.access_secret_id) == "gho_first"
    assert get_secret_value(session, connection.refresh_secret_id) == "refresh_first"
    # Nothing token-shaped is on the row itself.
    assert "gho_first" not in connection.model_dump_json()


def test_reauthorizing_replaces_in_place(session, test_org, test_user):
    first = _save(session, test_org, test_user)
    second = _save(session, test_org, test_user, access_token_value="gho_second")
    assert first.id == second.id
    assert len(list_connections(session, org_id=test_org.id, user_id=test_user.id)) == 1
    assert get_secret_value(session, second.access_secret_id) == "gho_second"


def test_a_reconsent_without_a_refresh_token_keeps_the_stored_one(session, test_org, test_user):
    """Google omits the refresh token on re-consent. Blanking it here would
    quietly make the connection single-use."""
    _save(session, test_org, test_user)
    connection = _save(
        session, test_org, test_user, access_token_value="gho_new", refresh_token_value=None
    )
    assert get_secret_value(session, connection.refresh_secret_id) == "refresh_first"


def test_disconnecting_destroys_both_secrets(session, test_org, test_user):
    connection = _save(session, test_org, test_user)
    access_id, refresh_id = connection.access_secret_id, connection.refresh_secret_id
    delete_connection(session, connection)
    session.commit()
    assert session.get(Secret, access_id) is None
    assert session.get(Secret, refresh_id) is None
    assert (
        find_connection(session, org_id=test_org.id, user_id=test_user.id, provider="github")
        is None
    )


def test_metadata_round_trips(session, test_org, test_user):
    connection = _save(
        session, test_org, test_user, provider="aws", metadata={"sso_region": "eu-west-1"}
    )
    assert metadata_of(connection)["sso_region"] == "eu-west-1"
    assert serialize(connection)["metadata"]["sso_region"] == "eu-west-1"


# ── access_token ─────────────────────────────────────────────────────────────


async def test_a_live_token_is_returned_untouched(session, test_org, test_user):
    connection = _save(
        session, test_org, test_user, expires=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert await access_token(session, connection) == "gho_first"


async def test_a_token_with_no_expiry_is_never_refreshed(session, test_org, test_user):
    connection = _save(session, test_org, test_user, expires=None)
    assert await access_token(session, connection) == "gho_first"


async def test_an_expired_token_is_refreshed_and_persisted(
    session, test_org, test_user, monkeypatch
):
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_id", "id")
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_secret", "secret")

    class Patched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"access_token": "gho_refreshed", "expires_in": 3600}
                )
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("sutr.connections.flow.httpx.AsyncClient", Patched)

    connection = _save(
        session, test_org, test_user, expires=datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    assert await access_token(session, connection) == "gho_refreshed"
    session.refresh(connection)
    assert get_secret_value(session, connection.access_secret_id) == "gho_refreshed"


async def test_an_expired_token_with_nothing_to_refresh_asks_for_reauthorization(
    session, test_org, test_user
):
    """The AWS device-grant case: the session simply ends."""
    connection = _save(
        session,
        test_org,
        test_user,
        provider="aws",
        refresh_token_value=None,
        expires=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    with pytest.raises(ConnectError) as excinfo:
        await access_token(session, connection)
    assert excinfo.value.code == "reauthorization_required"


async def test_a_connection_whose_secret_vanished_is_reported_not_returned(
    session, test_org, test_user
):
    connection = _save(session, test_org, test_user)
    connection.access_secret_id = None
    session.add(connection)
    session.commit()
    with pytest.raises(ConnectError) as excinfo:
        await access_token(session, connection)
    assert excinfo.value.code == "connection_broken"


def test_the_swapped_variable_name_is_named_outright(monkeypatch):
    """OAUTH_GITHUB_* (integration) and GITHUB_OAUTH_* (connected account)
    differ only in word order and authorize different things, so setting the
    wrong one must not read as "you set nothing"."""
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_id", "")
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_secret", "")
    monkeypatch.setenv("OAUTH_GITHUB_CLIENT_ID", "Ov23lidExample")

    configured, reason = is_configured("github")
    assert configured is False
    assert "OAUTH_GITHUB_CLIENT_ID is set" in reason
    assert "Connected accounts need GITHUB_OAUTH_CLIENT_ID" in reason


def test_no_swapped_name_hint_when_it_is_simply_unset(monkeypatch):
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_id", "")
    monkeypatch.setattr("sutr.config.settings.github_oauth_client_secret", "")
    monkeypatch.delenv("OAUTH_GITHUB_CLIENT_ID", raising=False)

    _configured, reason = is_configured("github")
    assert "OAUTH_GITHUB_CLIENT_ID is set" not in reason
