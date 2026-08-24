from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv(override=True)


class Settings(BaseSettings):
    dev: bool = False
    log_level: str = "INFO"
    database_url: str = "sqlite:///sutr.db"
    port: int = 4747
    base_url: str = "http://localhost:4747"
    oauth_callback_url: str = "http://localhost:4747/api/auth/callback"
    is_self_hosted: bool = False
    is_cloud: bool = False
    block_signups: bool = False
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7
    # Separate, deliberately short TTL for admin-to-user impersonation tokens.
    # A stolen impersonation bearer grants full account access until this
    # window expires, so keep it under an hour.
    impersonation_ttl_minutes: int = 30
    ui_base_url: str = "http://localhost:5173"
    approval_expiry_minutes: int = 10
    # Long-poll budget for sutr__await_approval. Kept under the typical
    # MCP client 300s request timeout so the agent gets a graceful "still
    # pending" response and can loop back in without the client disconnecting.
    approval_long_poll_timeout_seconds: int = 240

    # Email
    resend_api_key: str = ""
    email_from: str = "noreply@example.com"
    skip_email_verification: bool = False

    # PostHog
    posthog_project_token: str = ""
    posthog_host: str = "https://us.i.posthog.com"

    # Google sign-in (login with Google for the sutr UI). Completely
    # independent from the Google integration's OAuth credentials.
    google_login_client_id: str = ""
    google_login_client_secret: str = ""

    # ── Connected accounts (OAuth) ────────────────────────────────────────
    # One OAuth app per provider, registered once by whoever runs this
    # server. A provider with no client id is reported as unconfigured and
    # its "Connect" button is disabled — never silently broken.
    #
    # GitHub: an OAuth app (Settings → Developer settings → OAuth Apps) whose
    # callback is {base_url}/api/connections/github/callback. Used to read
    # OpenAPI specs out of repositories the user can already see.
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    # Scope requested for GitHub. "repo" is needed to read private
    # repositories; "public_repo" or "" suffices for public-only installs.
    github_oauth_scope: str = "repo read:user"

    # Google Cloud deployments. Separate from google_login_* on purpose: this
    # app requests cloud-platform, which is a far larger grant than sign-in.
    gcp_oauth_client_id: str = ""
    gcp_oauth_client_secret: str = ""

    # Azure deployments (Microsoft identity platform). "organizations" lets any
    # work/school tenant consent; pin a tenant id for a single-tenant install.
    azure_oauth_client_id: str = ""
    azure_oauth_client_secret: str = ""
    azure_oauth_tenant: str = "organizations"

    # AWS deployments via IAM Identity Center. AWS has no OAuth for its own
    # APIs, so this is the OIDC device authorization grant against sso-oidc:
    # the client is registered dynamically, which is why there is no secret
    # here. The start URL looks like https://d-abc123.awsapps.com/start.
    aws_sso_start_url: str = ""
    aws_sso_region: str = "us-east-1"

    # Deployment engine: allow the local Docker provider (self-hosted only —
    # the registry additionally refuses it whenever is_cloud is set).
    deploy_docker_enabled: bool = True
    # Cloud providers. Each additionally requires its OAuth app to be
    # configured above; enabling without credentials surfaces as "not
    # configured" rather than a failed deploy.
    deploy_gcp_enabled: bool = True
    deploy_azure_enabled: bool = True
    deploy_aws_enabled: bool = True

    # Tool executions allowed per org per minute, enforced in the shared
    # pipeline so REST and MCP are limited alike. 0 disables the limit.
    tool_rate_limit_per_minute: int = 120

    # Observability. /metrics is off by default: it is an infrastructure
    # endpoint, so it must be a deliberate choice to expose it. When enabled,
    # setting METRICS_TOKEN additionally requires `Authorization: Bearer <token>`.
    metrics_enabled: bool = False
    metrics_token: str = ""
    # Tracing needs the optional SDK: uv sync --extra otel
    otel_enabled: bool = False
    otel_service_name: str = "sutr"
    otel_exporter_otlp_endpoint: str = ""

    # Secrets backend: "db" (default) or "db_kms".
    secrets_backend: str = "db"
    # AWS KMS options (only used when secrets_backend = "db_kms").
    secrets_kms_key_id: str = ""
    secrets_kms_region: str = ""

    # Stripe billing (cloud only — self-hosted installs leave stripe_api_key empty).
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_plus: str = ""
    enterprise_contact_email: str = "sales@skaldlabs.io"

    def billing_enabled(self) -> bool:
        return self.is_cloud and bool(self.stripe_api_key) and bool(self.stripe_price_plus)

    def get_oauth_credentials(self, provider: str) -> tuple[str, str] | None:
        """Look up OAuth client_id and client_secret for a provider."""
        import os

        prefix = f"OAUTH_{provider.upper()}_"
        client_id = os.environ.get(f"{prefix}CLIENT_ID", "")
        client_secret = os.environ.get(f"{prefix}CLIENT_SECRET", "")
        if client_id and client_secret:
            return client_id, client_secret
        return None


settings = Settings()
