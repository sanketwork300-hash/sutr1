from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration


class GoogleMapsCodeAssistIntegration(RemoteMcpIntegration):
    """Google's hosted Maps Code Assist MCP server (2 tools).

    A documentation and code-generation aid for the Maps Platform, not a
    geocoding or routing API: it returns instructions and docs, not map
    data. `retrieve-instructions` is meant to be called first.

    Unlike the Workspace servers this one is a Cloud API, so it takes the
    `cloud-platform` scope."""

    id: str = "google_maps_code_assist"
    name: str = "Google Maps Code Assist"
    description: str = "Search Maps Platform docs, samples, and implementation guidance"
    docs_url: str = "https://developers.google.com/maps/code-assist"
    url: str = "https://mapscodeassist.googleapis.com/mcp"
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
