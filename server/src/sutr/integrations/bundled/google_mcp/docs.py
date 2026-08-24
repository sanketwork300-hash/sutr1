from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration


class GoogleDocsIntegration(RemoteMcpIntegration):
    """Google's hosted Docs MCP server (2 tools: read_doc, update_doc).

    Both take a document ID, which this server has no tool to find — pair it
    with Google Drive's `search_files` to locate documents first."""

    id: str = "google_docs"
    name: str = "Google Docs"
    description: str = "Read and batch-update Google Docs documents"
    docs_url: str = "https://developers.google.com/workspace/docs/mcp"
    url: str = "https://docsmcp.googleapis.com/mcp/v1"
    auth: list[AuthMethod] = Field(
        default=[
            google_oauth(
                [
                    "https://www.googleapis.com/auth/documents",
                ]
            )
        ]
    )

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
