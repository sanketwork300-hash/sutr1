from pydantic import Field

from sutr.integrations.bundled.google_mcp.oauth import google_oauth, google_oauth_available
from sutr.integrations.types import AuthMethod, RemoteMcpIntegration

_TOOL_CATEGORIES: dict[str, str] = {
    "list_events": "Events",
    "get_event": "Events",
    "search_events": "Events",
    "create_event": "Events",
    "update_event": "Events",
    "delete_event": "Events",
    "respond_to_event": "Events",
    "list_calendars": "Calendars",
    "suggest_time": "Scheduling",
}


class GoogleCalendarMcpIntegration(RemoteMcpIntegration):
    """Google's hosted Calendar MCP server (9 tools).

    Distinct from the `google_calendar` REST integration. This one adds two
    things that one has no equivalent for: `suggest_time` across several
    calendars, and `search_events` by semantic query rather than by field."""

    id: str = "google_calendar_mcp"
    name: str = "Google Calendar (Google MCP)"
    description: str = "Google's hosted Calendar server, with semantic event search"
    docs_url: str = "https://developers.google.com/workspace/calendar/mcp"
    url: str = "https://calendarmcp.googleapis.com/mcp/v1"
    auth: list[AuthMethod] = Field(
        default=[
            google_oauth(
                [
                    "https://www.googleapis.com/auth/calendar",
                ]
            )
        ]
    )
    tool_categories: dict[str, str] = Field(default=_TOOL_CATEGORIES)

    def is_available(self) -> tuple[bool, str | None]:
        return google_oauth_available()
