"""Google's own hosted MCP servers, one file per product.

These are first-party remote MCP endpoints Google runs at `*.googleapis.com`.
They are `remote_mcp` integrations, so Sutr proxies to them rather than
reimplementing the API — which is why they need no tool definitions here, only
a URL and the OAuth scopes their product requires.

Kept in their own package because two of them overlap with the REST-backed
integrations Sutr already ships (`gmail`, `google_calendar`), and the pair are
not interchangeable:

- `gmail` (REST) can **send** email. `gmail_mcp` cannot — Google's server
  publishes no send tool, only draft creation.
- `google_calendar_mcp` adds `suggest_time` and semantic `search_events`,
  which the REST integration has no equivalent for.

Both are offered rather than one replacing the other, so that installing this
never silently changes the tools an existing agent depends on.

Every URL and tool list here was read off the live servers with `tools/list`,
not transcribed from documentation.
"""

from sutr.integrations.bundled.google_mcp.calendar import GoogleCalendarMcpIntegration
from sutr.integrations.bundled.google_mcp.chat import GoogleChatIntegration
from sutr.integrations.bundled.google_mcp.contacts import GoogleContactsIntegration
from sutr.integrations.bundled.google_mcp.developer_knowledge import (
    GoogleDeveloperKnowledgeIntegration,
)
from sutr.integrations.bundled.google_mcp.docs import GoogleDocsIntegration
from sutr.integrations.bundled.google_mcp.drive import GoogleDriveIntegration
from sutr.integrations.bundled.google_mcp.gmail import GmailMcpIntegration
from sutr.integrations.bundled.google_mcp.maps_code_assist import (
    GoogleMapsCodeAssistIntegration,
)
from sutr.integrations.bundled.google_mcp.sheets import GoogleSheetsIntegration
from sutr.integrations.bundled.google_mcp.slides import GoogleSlidesIntegration

#: Instantiated in registry order so adding a product touches one list.
GOOGLE_MCP_INTEGRATIONS = [
    GmailMcpIntegration,
    GoogleCalendarMcpIntegration,
    GoogleChatIntegration,
    GoogleContactsIntegration,
    GoogleDeveloperKnowledgeIntegration,
    GoogleDocsIntegration,
    GoogleDriveIntegration,
    GoogleMapsCodeAssistIntegration,
    GoogleSheetsIntegration,
    GoogleSlidesIntegration,
]

__all__ = [
    "GOOGLE_MCP_INTEGRATIONS",
    "GmailMcpIntegration",
    "GoogleCalendarMcpIntegration",
    "GoogleChatIntegration",
    "GoogleContactsIntegration",
    "GoogleDeveloperKnowledgeIntegration",
    "GoogleDocsIntegration",
    "GoogleDriveIntegration",
    "GoogleMapsCodeAssistIntegration",
    "GoogleSheetsIntegration",
    "GoogleSlidesIntegration",
]
