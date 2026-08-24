from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration


class GoogleSlidesIntegration(RemoteMcpIntegration):
    """Google's hosted Slides MCP server (2 tools: read_presentation,
    update_presentation). Addressed by presentation ID; use Google Drive to
    find one."""

    id: str = "google_slides"
    name: str = "Google Slides"
    description: str = "Read and batch-update Slides presentations"
    docs_url: str = "https://developers.google.com/workspace/slides/mcp"
    url: str = "https://slidesmcp.googleapis.com/mcp/v1"
    auth: list[AuthMethod] = Field(
        default=[
            google_oauth(
                [
                    "https://www.googleapis.com/auth/presentations",
                ]
            )
        ]
    )

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
