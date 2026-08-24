from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration

_TOOL_CATEGORIES: dict[str, str] = {
    "get_values": "Read",
    "get_spreadsheet": "Read",
    "update_values": "Write",
    "update_formulas": "Write",
    "update_spreadsheet": "Write",
    "insert_dimension": "Structure",
}


class GoogleSheetsIntegration(RemoteMcpIntegration):
    """Google's hosted Sheets MCP server (6 tools).

    Like Docs, every tool is addressed by spreadsheet ID; use Google Drive
    to find one."""

    id: str = "google_sheets"
    name: str = "Google Sheets"
    description: str = "Read and write spreadsheet values, formulas, and structure"
    docs_url: str = "https://developers.google.com/workspace/sheets/mcp"
    url: str = "https://sheetsmcp.googleapis.com/mcp/v1"
    auth: list[AuthMethod] = Field(
        default=[
            google_oauth(
                [
                    "https://www.googleapis.com/auth/spreadsheets",
                ]
            )
        ]
    )
    tool_categories: dict[str, str] = Field(default=_TOOL_CATEGORIES)

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
