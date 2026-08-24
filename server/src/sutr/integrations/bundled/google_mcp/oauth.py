"""Google OAuth wiring shared by Google's own hosted MCP servers.

These servers live at `*.googleapis.com` behind Google's standard API frontend,
so they authenticate exactly like any other Google API: a normal OAuth 2 access
token in `Authorization: Bearer`. There is no MCP-specific discovery, no
protected-resource metadata, and no dynamic client registration — probing them
returns neither `WWW-Authenticate` nor `/.well-known/oauth-protected-resource`,
and an unauthenticated `tools/call` answers "Expected OAuth 2 access token".

So each integration only has to say which scopes its product needs. Scopes are
per-product on purpose: installing Sheets should not hand out mailbox access.
"""

from sutr.integrations.types import OAuthAuth

AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# offline + consent is the only combination that reliably yields a refresh
# token from Google, and an integration that stops working in an hour is not
# an integration. Matches the existing Gmail/Calendar REST integrations.
_EXTRA_AUTH_PARAMS = {"access_type": "offline", "prompt": "consent"}


def google_oauth(scopes: list[str]) -> OAuthAuth:
    return OAuthAuth(
        method="oauth",
        provider="google",
        authorization_url=AUTHORIZATION_URL,
        token_url=TOKEN_URL,
        scopes=scopes,
        extra_auth_params=_EXTRA_AUTH_PARAMS,
    )


def google_oauth_available() -> tuple[bool, str | None]:
    """Every Google integration needs one shared OAuth app to be registered.

    Kept identical to the message the REST Gmail and Calendar integrations
    already use, so a user who has not set this up reads one instruction
    rather than several.
    """
    from sutr.config import settings

    if settings.get_oauth_credentials("google"):
        return True, None
    return (
        False,
        "Set the env vars OAUTH_GOOGLE_CLIENT_ID and "
        "OAUTH_GOOGLE_CLIENT_SECRET to use this integration.",
    )
