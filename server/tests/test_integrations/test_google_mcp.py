"""Google's hosted MCP servers as bundled integrations.

The endpoints and tool lists these describe were read off the live servers with
`tools/list` on 2026-08-21. Nothing here touches the network: these tests guard
the properties that would silently rot — an id collision with the REST Gmail or
Calendar integrations, a scope that grants more than the product needs, or a
copy-paste that points two products at one URL.
"""

import pytest

from sutr.integrations.bundled.google_mcp import GOOGLE_MCP_INTEGRATIONS
from sutr.integrations.registry import _INTEGRATIONS
from sutr.integrations.types import OAuthAuth, RemoteMcpIntegration

GOOGLE_MCP_IDS = {
    "gmail_mcp",
    "google_calendar_mcp",
    "google_chat",
    "google_contacts",
    "google_developer_knowledge",
    "google_docs",
    "google_drive",
    "google_maps_code_assist",
    "google_sheets",
    "google_slides",
}


@pytest.fixture(name="google_oauth_configured")
def google_oauth_configured_fixture(monkeypatch):
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_SECRET", "test-client-secret")


def _instances():
    return [integration() for integration in GOOGLE_MCP_INTEGRATIONS]


def test_all_ten_are_registered_in_the_catalog():
    assert GOOGLE_MCP_IDS <= set(_INTEGRATIONS)


def test_they_do_not_collide_with_the_rest_backed_google_integrations():
    """`gmail` and `google_calendar` are Sutr's REST integrations and stay put.

    Replacing them would change tool names under anyone who has already
    installed them, silently invalidating their per-tool approval policies.
    """
    assert _INTEGRATIONS["gmail"].type == "custom"
    assert _INTEGRATIONS["google_calendar"].type == "custom"
    assert _INTEGRATIONS["gmail_mcp"].type == "remote_mcp"
    assert _INTEGRATIONS["google_calendar_mcp"].type == "remote_mcp"


def test_every_id_and_url_is_unique():
    instances = _instances()
    ids = [i.id for i in instances]
    urls = [i.url for i in instances]
    assert len(set(ids)) == len(ids)
    assert len(set(urls)) == len(urls)


@pytest.mark.parametrize("integration", _instances(), ids=lambda i: i.id)
def test_each_is_a_google_hosted_remote_mcp_endpoint(integration):
    assert isinstance(integration, RemoteMcpIntegration)
    assert integration.url.startswith("https://")
    assert ".googleapis.com/mcp" in integration.url


@pytest.mark.parametrize("integration", _instances(), ids=lambda i: i.id)
def test_each_uses_google_oauth_with_a_refreshable_grant(integration):
    assert len(integration.auth) == 1
    auth = integration.auth[0]
    assert isinstance(auth, OAuthAuth)
    assert auth.provider == "google"
    assert auth.token_url == "https://oauth2.googleapis.com/token"
    # Without offline+consent Google issues no refresh token, and the
    # integration would stop working within the hour.
    assert auth.extra_auth_params == {"access_type": "offline", "prompt": "consent"}
    assert auth.scopes


@pytest.mark.parametrize("integration", _instances(), ids=lambda i: i.id)
def test_scopes_are_scoped_to_the_product(integration):
    """Installing Sheets must not hand out mailbox access."""
    scopes = integration.auth[0].scopes or []
    if integration.id == "gmail_mcp":
        assert scopes == ["https://www.googleapis.com/auth/gmail.modify"]
    else:
        assert not any("gmail" in scope for scope in scopes)
    # cloud-platform is broad; only the two Cloud APIs may ask for it.
    if any("cloud-platform" in scope for scope in scopes):
        assert integration.id in {"google_maps_code_assist", "google_developer_knowledge"}


@pytest.mark.parametrize("integration", _instances(), ids=lambda i: i.id)
def test_unavailable_until_the_shared_google_oauth_app_is_configured(integration, monkeypatch):
    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_SECRET", raising=False)
    available, reason = integration.is_available()
    assert available is False
    assert "OAUTH_GOOGLE_CLIENT_ID" in reason


@pytest.mark.parametrize("integration", _instances(), ids=lambda i: i.id)
def test_available_once_configured(integration, google_oauth_configured):
    assert integration.is_available() == (True, None)


@pytest.mark.parametrize("integration", _instances(), ids=lambda i: i.id)
def test_each_carries_catalog_metadata(integration):
    assert integration.name
    assert integration.description
    assert integration.docs_url.startswith("https://")


def test_tool_categories_only_name_tools_the_server_actually_publishes():
    """Guards against a category map drifting from the live tool list.

    The right-hand side is what `tools/list` returned on 2026-08-21; a category
    for a tool that does not exist would silently never group anything.
    """
    published = {
        "gmail_mcp": {
            "create_draft",
            "list_drafts",
            "get_thread",
            "get_message",
            "search_threads",
            "label_thread",
            "unlabel_thread",
            "apply_sensitive_thread_label",
            "trash_thread",
            "untrash_thread",
            "mark_thread_spam",
            "unmark_thread_spam",
            "list_labels",
            "label_message",
            "unlabel_message",
            "apply_sensitive_message_label",
            "trash_message",
            "untrash_message",
            "mark_message_spam",
            "unmark_message_spam",
            "create_label",
        },
        "google_drive": {
            "copy_file",
            "create_file",
            "download_file_content",
            "get_file_metadata",
            "get_file_permissions",
            "list_recent_files",
            "read_file_content",
            "search_files",
        },
        "google_sheets": {
            "get_values",
            "get_spreadsheet",
            "update_spreadsheet",
            "update_values",
            "update_formulas",
            "insert_dimension",
        },
        "google_calendar_mcp": {
            "list_events",
            "get_event",
            "list_calendars",
            "suggest_time",
            "create_event",
            "update_event",
            "delete_event",
            "respond_to_event",
            "search_events",
        },
        "google_chat": {
            "list_messages",
            "search_messages",
            "search_conversations",
            "send_message",
        },
    }
    for integration in _instances():
        expected = published.get(integration.id)
        if expected is None:
            continue
        assert set(integration.tool_categories) <= expected, integration.id
        # Where a map exists it should cover the whole server.
        assert set(integration.tool_categories) == expected, integration.id


def test_gmail_mcp_is_documented_as_unable_to_send():
    """Google's Gmail server publishes no send tool; the REST `gmail` one does.

    If Google later adds sending, this test failing is the prompt to update the
    description rather than leaving users told something untrue.
    """
    gmail_mcp = _INTEGRATIONS["gmail_mcp"]
    assert "no sending" in gmail_mcp.description.lower()
    assert "send" not in gmail_mcp.tool_categories
