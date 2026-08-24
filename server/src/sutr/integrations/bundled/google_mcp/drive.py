from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration

_TOOL_CATEGORIES: dict[str, str] = {
    "search_files": "Find",
    "list_recent_files": "Find",
    "get_file_metadata": "Find",
    "get_file_permissions": "Find",
    "read_file_content": "Read",
    "download_file_content": "Read",
    "create_file": "Write",
    "copy_file": "Write",
}


class GoogleDriveIntegration(RemoteMcpIntegration):
    """Google's hosted Drive MCP server (8 tools).

    `create_file` and `copy_file` write, so this asks for the full `drive`
    scope rather than `drive.readonly`. An install that only ever needs to
    read is better served by denying the write tools in policy than by
    narrowing the scope, because the server offers them either way."""

    id: str = "google_drive"
    name: str = "Google Drive"
    description: str = "Search, read, create, and copy files in Drive"
    docs_url: str = "https://developers.google.com/workspace/drive/mcp"
    url: str = "https://drivemcp.googleapis.com/mcp/v1"
    auth: list[AuthMethod] = Field(
        default=[
            google_oauth(
                [
                    "https://www.googleapis.com/auth/drive",
                ]
            )
        ]
    )
    tool_categories: dict[str, str] = Field(default=_TOOL_CATEGORIES)

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
