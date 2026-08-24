"""Which external accounts sutr can be connected to, and how.

Everything provider-specific about the *authorization* lives here so
`flow.py` and `device.py` stay generic. Nothing here touches the database or
performs I/O: a descriptor is pure configuration, resolved from settings so a
self-hosted install can point at its own OAuth apps.

Two flows are represented:

- ``authorization_code`` - GitHub, Google Cloud, Azure. Browser redirect,
  PKCE where the provider supports it, refresh token where offered.
- ``device`` - AWS. AWS exposes no OAuth for its own APIs; IAM Identity
  Center's ``sso-oidc`` service does implement the OAuth 2.0 device
  authorization grant (RFC 8628), and the token it returns is exchanged for
  short-lived STS credentials. See `device.py`.
"""

from dataclasses import dataclass, field

from sutr.config import settings

SOURCE_PROVIDERS = ("github",)
DEPLOY_PROVIDERS = ("gcp", "azure", "aws")


@dataclass(frozen=True)
class OAuthProvider:
    id: str
    display_name: str
    # "source" - where specifications come from; "deploy" - where servers run.
    kind: str
    flow: str
    scopes: str
    authorize_url: str = ""
    token_url: str = ""
    # PKCE is on wherever the provider accepts it. GitHub OAuth apps ignore
    # the challenge rather than rejecting it, so it is harmless there too.
    uses_pkce: bool = True
    # Sent verbatim on the authorization request.
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    # Where the operator registers the OAuth app. Quoted in the "not
    # configured" error so the fix is one click away.
    setup_url: str = ""
    # What the grant actually permits, in one sentence, shown next to the
    # Connect button. The user is about to hand over real access; saying
    # "authorize" without saying to what is how consent screens get clicked
    # through without being read.
    grant_summary: str = ""


def _github() -> OAuthProvider:
    return OAuthProvider(
        id="github",
        display_name="GitHub",
        kind="source",
        flow="authorization_code",
        scopes=settings.github_oauth_scope,
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        extra_authorize_params={"allow_signup": "false"},
        setup_url="https://github.com/settings/developers",
        grant_summary=(
            "Read the repositories you can already see, so sutr can list and "
            "fetch OpenAPI specifications from them. sutr never writes to a "
            "repository."
        ),
    )


def _gcp() -> OAuthProvider:
    return OAuthProvider(
        id="gcp",
        display_name="Google Cloud",
        kind="deploy",
        flow="authorization_code",
        scopes="openid email https://www.googleapis.com/auth/cloud-platform",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        # offline + consent is the only way Google returns a refresh token,
        # and a deployment that cannot be reconciled tomorrow is not a
        # deployment.
        extra_authorize_params={
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        },
        setup_url="https://console.cloud.google.com/apis/credentials",
        grant_summary=(
            "Create and manage Cloud Build builds, Artifact Registry images, "
            "and Cloud Run services in the project you choose. This is a broad "
            "grant: cloud-platform covers every Google Cloud API you can reach."
        ),
    )


def _azure() -> OAuthProvider:
    tenant = settings.azure_oauth_tenant or "organizations"
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    return OAuthProvider(
        id="azure",
        display_name="Microsoft Azure",
        kind="deploy",
        flow="authorization_code",
        # ARM's user_impersonation is what actually authorizes the deploy;
        # offline_access is what keeps it working past an hour.
        scopes="openid email offline_access https://management.azure.com/user_impersonation",
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        extra_authorize_params={"response_mode": "query", "prompt": "select_account"},
        setup_url="https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps",
        grant_summary=(
            "Act as you against Azure Resource Manager: create the container "
            "registry, build the image, and run the Container App in the "
            "subscription and resource group you choose."
        ),
    )


def _aws() -> OAuthProvider:
    return OAuthProvider(
        id="aws",
        display_name="Amazon Web Services",
        kind="deploy",
        flow="device",
        scopes="sso:account:access",
        uses_pkce=False,
        setup_url="https://console.aws.amazon.com/singlesignon/home",
        grant_summary=(
            "Assume the IAM Identity Center role you pick, in the account you "
            "pick, for as long as that role's session lasts. sutr stores only "
            "the Identity Center token; the AWS credentials it exchanges for "
            "are short-lived and re-fetched on every operation."
        ),
    )


_BUILDERS = {
    "github": _github,
    "gcp": _gcp,
    "azure": _azure,
    "aws": _aws,
}

ALL_PROVIDERS = tuple(_BUILDERS)


def get_provider(provider_id: str) -> OAuthProvider | None:
    builder = _BUILDERS.get(provider_id)
    return builder() if builder else None


def client_credentials(provider_id: str) -> tuple[str, str]:
    """(client_id, client_secret) for an authorization-code provider."""
    if provider_id == "github":
        return settings.github_oauth_client_id, settings.github_oauth_client_secret
    if provider_id == "gcp":
        return settings.gcp_oauth_client_id, settings.gcp_oauth_client_secret
    if provider_id == "azure":
        return settings.azure_oauth_client_id, settings.azure_oauth_client_secret
    return "", ""


def callback_url(provider_id: str) -> str:
    """The redirect URI registered with the provider. Must match exactly."""
    return f"{settings.base_url.rstrip('/')}/api/connections/{provider_id}/callback"


def is_configured(provider_id: str) -> tuple[bool, str | None]:
    """Can this server start a flow for the provider at all?

    Answered before any button is drawn, so an unconfigured provider reads as
    "the operator has not set this up" instead of failing at the redirect.
    """
    provider = get_provider(provider_id)
    if provider is None:
        return False, f"Unknown provider '{provider_id}'."
    if provider.flow == "device":
        if not settings.aws_sso_start_url:
            return False, (
                "AWS_SSO_START_URL is not set on this server. Point it at your "
                "IAM Identity Center portal, e.g. "
                "https://d-1234567890.awsapps.com/start."
            )
        return True, None
    client_id, client_secret = client_credentials(provider_id)
    if not client_id or not client_secret:
        env = provider_id.upper()
        reason = (
            f"{env}_OAUTH_CLIENT_ID and {env}_OAUTH_CLIENT_SECRET are not set on "
            f"this server. Register an OAuth app at {provider.setup_url} with the "
            f"callback {callback_url(provider_id)}."
        )
        if _swapped_name_is_set(provider_id):
            # The integration convention is OAUTH_<PROVIDER>_*, the connected
            # account convention is <PROVIDER>_OAUTH_*. They differ only in
            # word order and authorize completely different things, so the one
            # mistake worth naming outright is having set the other one.
            reason += (
                f" OAUTH_{env}_CLIENT_ID is set, but that is the credential for "
                f"connecting the bundled {provider.display_name} integration, "
                f"which uses a different callback. Connected accounts need "
                f"{env}_OAUTH_CLIENT_ID."
            )
        return False, reason
    return True, None


def _swapped_name_is_set(provider_id: str) -> bool:
    """Has the operator set OAUTH_<PROVIDER>_* instead of <PROVIDER>_OAUTH_*?"""
    import os

    return bool(os.environ.get(f"OAUTH_{provider_id.upper()}_CLIENT_ID"))
