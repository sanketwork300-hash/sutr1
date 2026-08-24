from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration


class GoogleContactsIntegration(RemoteMcpIntegration):
    """Google's hosted People MCP server (3 tools).

    Read-only, so the scopes are the `.readonly` variants.
    `search_directory_people` works only on Google Workspace accounts — on a
    consumer account it returns nothing rather than failing."""

    id: str = "google_contacts"
    name: str = "Google Contacts"
    description: str = "Look up contacts, your Workspace directory, and your own profile"
    docs_url: str = "https://developers.google.com/people"
    url: str = "https://people.googleapis.com/mcp/v1"
    auth: list[AuthMethod] = Field(
        default=[
            google_oauth(
                [
                    "https://www.googleapis.com/auth/contacts.readonly",
                    "https://www.googleapis.com/auth/directory.readonly",
                    "https://www.googleapis.com/auth/userinfo.profile",
                    "https://www.googleapis.com/auth/userinfo.email",
                ]
            )
        ]
    )

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
