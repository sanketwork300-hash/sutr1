from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration

_TOOL_CATEGORIES: dict[str, str] = {
    "list_messages": "Messages",
    "search_messages": "Messages",
    "send_message": "Messages",
    "search_conversations": "Conversations",
}


class GoogleChatIntegration(RemoteMcpIntegration):
    """Google's hosted Chat MCP server (4 tools).

    Needs two scopes: `chat.messages` to read and send, and
    `chat.spaces.readonly` for `search_conversations` to resolve a space by
    name. Messages are exchanged as Markdown."""

    id: str = "google_chat"
    name: str = "Google Chat"
    description: str = "Read, search, and send Google Chat messages"
    docs_url: str = "https://developers.google.com/workspace/chat/mcp"
    url: str = "https://chatmcp.googleapis.com/mcp/v1"
    auth: list[AuthMethod] = Field(
        default=[
            google_oauth(
                [
                    "https://www.googleapis.com/auth/chat.messages",
                    "https://www.googleapis.com/auth/chat.spaces.readonly",
                ]
            )
        ]
    )
    tool_categories: dict[str, str] = Field(default=_TOOL_CATEGORIES)

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
