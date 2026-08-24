from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration

# Verified against the live server's tools/list on 2026-08-21 (21 tools).
_TOOL_CATEGORIES: dict[str, str] = {
    "search_threads": "Threads",
    "get_thread": "Threads",
    "label_thread": "Threads",
    "unlabel_thread": "Threads",
    "apply_sensitive_thread_label": "Threads",
    "trash_thread": "Threads",
    "untrash_thread": "Threads",
    "mark_thread_spam": "Threads",
    "unmark_thread_spam": "Threads",
    "get_message": "Messages",
    "label_message": "Messages",
    "unlabel_message": "Messages",
    "apply_sensitive_message_label": "Messages",
    "trash_message": "Messages",
    "untrash_message": "Messages",
    "mark_message_spam": "Messages",
    "unmark_message_spam": "Messages",
    "create_draft": "Drafts",
    "list_drafts": "Drafts",
    "list_labels": "Labels",
    "create_label": "Labels",
}


class GmailMcpIntegration(RemoteMcpIntegration):
    """Google's own hosted Gmail MCP server.

    Distinct from the `gmail` integration, which is Sutr's REST-backed one, and
    the difference is not cosmetic: **this server cannot send email.** It
    creates drafts, reads threads and messages, and manages labels, trash, and
    spam — there is no send tool in its published tool list. Use the `gmail`
    integration when an agent needs to actually send.
    """

    id: str = "gmail_mcp"
    name: str = "Gmail (Google MCP)"
    description: str = "Google's hosted Gmail server: drafts, threads, labels — no sending"
    docs_url: str = "https://developers.google.com/workspace/gmail/mcp"
    url: str = "https://gmailmcp.googleapis.com/mcp/v1"
    auth: list[AuthMethod] = Field(
        default=[google_oauth(["https://www.googleapis.com/auth/gmail.modify"])]
    )
    tool_categories: dict[str, str] = Field(default=_TOOL_CATEGORIES)

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
