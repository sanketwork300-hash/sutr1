import json

from sutr.integrations.bundled.amplitude import AmplitudeIntegration
from sutr.integrations.bundled.apify import ApifyIntegration
from sutr.integrations.bundled.asana import AsanaIntegration
from sutr.integrations.bundled.atlassian import AtlassianIntegration
from sutr.integrations.bundled.attio import AttioIntegration
from sutr.integrations.bundled.axiom import AxiomIntegration
from sutr.integrations.bundled.calendly import CalendlyIntegration
from sutr.integrations.bundled.close import CloseIntegration
from sutr.integrations.bundled.cloudflare import CloudflareIntegration
from sutr.integrations.bundled.cloudinary import CloudinaryIntegration
from sutr.integrations.bundled.contentful import ContentfulIntegration
from sutr.integrations.bundled.datadog import DatadogIntegration
from sutr.integrations.bundled.egnyte import EgnyteIntegration
from sutr.integrations.bundled.exa import ExaIntegration
from sutr.integrations.bundled.fireflies import FirefliesIntegration
from sutr.integrations.bundled.github import GitHubIntegration
from sutr.integrations.bundled.gmail import GmailIntegration
from sutr.integrations.bundled.google_calendar import GoogleCalendarIntegration
from sutr.integrations.bundled.google_mcp import GOOGLE_MCP_INTEGRATIONS
from sutr.integrations.bundled.granola import GranolaIntegration
from sutr.integrations.bundled.huggingface import HuggingFaceIntegration
from sutr.integrations.bundled.intercom import IntercomIntegration
from sutr.integrations.bundled.launchdarkly import LaunchDarklyIntegration
from sutr.integrations.bundled.linear import LinearIntegration
from sutr.integrations.bundled.mercury import MercuryIntegration
from sutr.integrations.bundled.mixpanel import MixpanelIntegration
from sutr.integrations.bundled.monday import MondayIntegration
from sutr.integrations.bundled.neon import NeonIntegration
from sutr.integrations.bundled.netlify import NetlifyIntegration
from sutr.integrations.bundled.notion import NotionIntegration
from sutr.integrations.bundled.posthog import PostHogIntegration
from sutr.integrations.bundled.prisma import PrismaIntegration
from sutr.integrations.bundled.ramp import RampIntegration
from sutr.integrations.bundled.render import RenderIntegration
from sutr.integrations.bundled.resend import ResendIntegration
from sutr.integrations.bundled.sanity import SanityIntegration
from sutr.integrations.bundled.semgrep import SemgrepIntegration
from sutr.integrations.bundled.sentry import SentryIntegration
from sutr.integrations.bundled.slack import SlackIntegration
from sutr.integrations.bundled.square import SquareIntegration
from sutr.integrations.bundled.stripe import StripeIntegration
from sutr.integrations.bundled.stytch import StytchIntegration
from sutr.integrations.bundled.supabase import SupabaseIntegration
from sutr.integrations.bundled.tally import TallyIntegration
from sutr.integrations.bundled.telnyx import TelnyxIntegration
from sutr.integrations.bundled.thoughtspot import ThoughtSpotIntegration
from sutr.integrations.bundled.vercel import VercelIntegration
from sutr.integrations.bundled.webflow import WebflowIntegration
from sutr.integrations.bundled.wix import WixIntegration
from sutr.integrations.bundled.zapier import ZapierIntegration
from sutr.integrations.types import (
    ApiTool,
    CustomIntegration,
    Integration,
    OAuthAuth,
    RemoteMcpIntegration,
    TokenAuth,
)

_INTEGRATIONS: dict[str, Integration] = {
    i.id: i
    for i in [
        AmplitudeIntegration(),
        ApifyIntegration(),
        AsanaIntegration(),
        AtlassianIntegration(),
        AttioIntegration(),
        AxiomIntegration(),
        CalendlyIntegration(),
        CloseIntegration(),
        CloudflareIntegration(),
        CloudinaryIntegration(),
        ContentfulIntegration(),
        DatadogIntegration(),
        EgnyteIntegration(),
        ExaIntegration(),
        FirefliesIntegration(),
        GitHubIntegration(),
        GranolaIntegration(),
        GmailIntegration(),
        GoogleCalendarIntegration(),
        # Google's own hosted MCP servers (Drive, Docs, Sheets, Chat, ...).
        # Kept as a list so adding a product touches one file, not this one.
        *(integration() for integration in GOOGLE_MCP_INTEGRATIONS),
        HuggingFaceIntegration(),
        IntercomIntegration(),
        LaunchDarklyIntegration(),
        LinearIntegration(),
        MercuryIntegration(),
        MixpanelIntegration(),
        MondayIntegration(),
        NeonIntegration(),
        NetlifyIntegration(),
        NotionIntegration(),
        PostHogIntegration(),
        PrismaIntegration(),
        RampIntegration(),
        RenderIntegration(),
        ResendIntegration(),
        SanityIntegration(),
        SemgrepIntegration(),
        SentryIntegration(),
        SlackIntegration(),
        SquareIntegration(),
        StripeIntegration(),
        StytchIntegration(),
        SupabaseIntegration(),
        TallyIntegration(),
        TelnyxIntegration(),
        ThoughtSpotIntegration(),
        VercelIntegration(),
        WebflowIntegration(),
        WixIntegration(),
        ZapierIntegration(),
    ]
}


CUSTOM_PREFIX = "custom_"
CUSTOM_API_PREFIX = "customapi_"


def _custom_row_to_integration(row) -> RemoteMcpIntegration:
    from sutr.integrations.types import AuthMethod

    auth: list[AuthMethod] = []
    if row.auth_method == "token":
        auth.append(
            TokenAuth(
                method="token",
                label="API token",
                header=row.token_header,
                format=row.token_format,
            )
        )
    elif row.auth_method == "oauth":
        # No provider set → install will fall through to MCP discovery + DCR
        # against the user's URL.
        auth.append(OAuthAuth(method="oauth"))

    return RemoteMcpIntegration(
        id=row.integration_id,
        name=row.name,
        description=row.description,
        url=row.url,
        auth=auth,
    )


def _load_custom_row(integration_id: str, org_id) -> RemoteMcpIntegration | None:
    from sqlmodel import Session, select

    from sutr.db import engine
    from sutr.models.custom_mcp_integration import CustomMcpIntegration

    with Session(engine) as session:
        row = session.exec(  # noqa: S608
            select(CustomMcpIntegration)
            .where(CustomMcpIntegration.org_id == org_id)
            .where(CustomMcpIntegration.integration_id == integration_id)
        ).first()
        if not row:
            return None
        return _custom_row_to_integration(row)


def _load_custom_rows_for_org(org_id) -> list[RemoteMcpIntegration]:
    from sqlmodel import Session, select

    from sutr.db import engine
    from sutr.models.custom_mcp_integration import CustomMcpIntegration

    with Session(engine) as session:
        rows = session.exec(  # noqa: S608
            select(CustomMcpIntegration).where(CustomMcpIntegration.org_id == org_id)
        ).all()
        return [_custom_row_to_integration(r) for r in rows]


def _custom_api_row_to_integration(row) -> CustomIntegration:
    from sutr.token_auth import is_no_auth

    tools = [ApiTool(**tool) for tool in json.loads(row.tools_json)]
    auth: list = []
    if not is_no_auth(row.token_header, row.token_format):
        auth.append(
            TokenAuth(
                method="token",
                label="API token",
                header=row.token_header,
                format=row.token_format,
            )
        )
    return CustomIntegration(
        id=row.integration_id,
        name=row.name,
        description=row.description,
        base_url=row.base_url,
        auth=auth,
        tools=tools,
    )


def _load_custom_api_row(integration_id: str, org_id) -> CustomIntegration | None:
    from sqlmodel import Session, select

    from sutr.db import engine
    from sutr.models.custom_api_integration import CustomApiIntegration

    with Session(engine) as session:
        row = session.exec(
            select(CustomApiIntegration)
            .where(CustomApiIntegration.org_id == org_id)
            .where(CustomApiIntegration.integration_id == integration_id)
        ).first()
        if not row:
            return None
        return _custom_api_row_to_integration(row)


def _load_custom_api_rows_for_org(org_id) -> list[CustomIntegration]:
    from sqlmodel import Session, select

    from sutr.db import engine
    from sutr.models.custom_api_integration import CustomApiIntegration

    with Session(engine) as session:
        rows = session.exec(
            select(CustomApiIntegration).where(CustomApiIntegration.org_id == org_id)
        ).all()
        return [_custom_api_row_to_integration(r) for r in rows]


def get(integration_id: str, org_id=None) -> Integration | None:
    """Look up an integration by id.

    Bundled integrations are always available. Custom (user-defined) integrations
    require an org_id; without it, ids beginning with custom_ return None.
    """
    if integration_id in _INTEGRATIONS:
        return _INTEGRATIONS[integration_id]
    if org_id is not None and integration_id.startswith(CUSTOM_API_PREFIX):
        return _load_custom_api_row(integration_id, org_id)
    if org_id is not None and integration_id.startswith(CUSTOM_PREFIX):
        return _load_custom_row(integration_id, org_id)
    return None


def list_all(org_id=None) -> list[Integration]:
    bundled = list(_INTEGRATIONS.values())
    if org_id is None:
        return bundled
    return bundled + _load_custom_api_rows_for_org(org_id) + _load_custom_rows_for_org(org_id)
