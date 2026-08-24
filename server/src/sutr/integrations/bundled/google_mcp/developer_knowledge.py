from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration


class GoogleDeveloperKnowledgeIntegration(RemoteMcpIntegration):
    """Google's hosted Developer Knowledge MCP server (3 tools).

    Grounded answers over official Google developer documentation.
    `answer_query` synthesises an answer and is explicitly documented as
    having limited quota, so it is worth leaving on require-approval while
    `search_documents` and `get_documents` are relaxed.

    A Cloud API, so it takes the `cloud-platform` scope."""

    id: str = "google_developer_knowledge"
    name: str = "Google Developer Knowledge"
    description: str = "Search and cite official Google developer documentation"
    docs_url: str = "https://developers.google.com/developer-knowledge"
    url: str = "https://developerknowledge.googleapis.com/mcp"
    auth: list[AuthMethod] = Field(
        default=[
            google_oauth(
                [
                    "https://www.googleapis.com/auth/cloud-platform",
                ]
            )
        ]
    )

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
