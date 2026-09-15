from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv(override=True)


class Settings(BaseSettings):
    dev: bool = False
    log_level: str = "INFO"
    # "json" emits one structured object per line carrying the ESDS LLD §5.3
    # field set (correlation/tenant/agent/provider/tool/runtime ids, status,
    # duration, region); "text" is the human-readable local development form.
    log_format: str = "json"
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

    # ── Continuous source sync (ESDS LLD §3.3) ────────────────────────────
    # Watching is opt-in per source; this only controls whether the sweep that
    # honours those choices runs at all. Off means no source is ever polled,
    # whatever its own setting says.
    source_sync_enabled: bool = True
    # How often to look for sources whose own interval is due — not how often
    # any one source is polled.
    source_sync_interval_seconds: int = 300

    # MCP transports. StreamableHTTP at /mcp is always on and is the default;
    # SSE (GET /sse + POST /messages) is the transport older MCP clients speak.
    mcp_sse_enabled: bool = True

    # ── Event bus (ESDS LLD §5.6, ADR-003) ────────────────────────────────
    # Events are always written to the transactional outbox; this chooses only
    # where the relay publishes them.
    #   in_process (default) — deliver to handlers in this process. Needs no
    #                          infrastructure, so the single-container install
    #                          keeps working.
    #   kafka                — publish to a broker. Needs KAFKA_BOOTSTRAP_SERVERS
    #                          and the optional client: uv sync --extra kafka
    event_bus_backend: str = "in_process"
    kafka_bootstrap_servers: str = ""
    # Prefixed onto every topic name, for sharing a cluster between environments.
    kafka_topic_prefix: str = ""
    # The relay runs in-process with the API by default. Set false to run it as
    # a separate worker instead (`uv run sutr-event-relay`), which is what a
    # multi-replica deployment wants so one relay owns publication.
    event_relay_enabled: bool = True

    # Emit a `usage.recorded` event per tool call. Off by default: the usage
    # ledger is already durable and authoritative, and nothing consumes the
    # event yet, so emitting one would double the write volume of every
    # invocation for no reader. Turn on when the Billing service exists.
    emit_usage_events: bool = False

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
    # Head sampling ratio for new traces. 1.0 keeps every trace, which is right
    # for a low-volume control plane and wrong for a busy gateway; lower it
    # there rather than turning tracing off. A trace started by a caller keeps
    # the caller's decision — a sampled request is not half-recorded here.
    otel_traces_sample_ratio: float = 1.0
    # mTLS to the collector (LLD §5.3). These are the paths the OpenTelemetry
    # specification's OTEL_EXPORTER_OTLP_CERTIFICATE /
    # OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE / OTEL_EXPORTER_OTLP_CLIENT_KEY
    # name: a CA bundle to verify the collector, and a client certificate and
    # key to authenticate to it. Unset ⇒ plain OTLP, and the capabilities
    # report says the hop is unauthenticated.
    otel_exporter_otlp_certificate: str = ""
    otel_exporter_otlp_client_certificate: str = ""
    otel_exporter_otlp_client_key: str = ""
    # How far back the tenant-scoped telemetry read model will look. A tenant
    # reads its own operational history from this install's tables; the window
    # bounds the scan rather than the retention (retention is separate).
    telemetry_window_hours_max: int = 24 * 30

    # Secrets backend: "db" (default) or "db_kms".
    secrets_backend: str = "db"
    # AWS KMS options (only used when secrets_backend = "db_kms").
    secrets_kms_key_id: str = ""
    secrets_kms_region: str = ""

    # Documentation intelligence (LLD §3.5).
    # Where uploaded documents are kept: "database" (default, alongside every
    # other blob this install stores) or "filesystem" (needs
    # DOCUMENT_STORAGE_PATH). There is no object-store backend yet — see
    # sutr/documentation/storage.py.
    document_storage_backend: str = "database"
    document_storage_path: str = ""
    # Upload ceiling. Parsing cost is superlinear in practice (OCR especially),
    # so this is a real limit, not a formality.
    document_max_bytes: int = 25 * 1024 * 1024
    # OCR is opt-in: it is slow, and on a scanned document it is the difference
    # between a job that takes a second and one that takes minutes.
    document_ocr_enabled: bool = True

    # MCP generation (LLD §3.6).
    # Artifact signing. The LLD asks for signed images via Cosign; this install
    # signs the *artifact build hash* with a locally held Ed25519 key instead,
    # which is a real signature over a real digest but is not Sigstore and does
    # not sign a container image (ADR-038). Unset ⇒ artifacts are unsigned and
    # say so.
    generation_signing_key: str = ""
    generation_signing_key_id: str = ""
    # Optional external gates. Each is a command run in a directory holding the
    # unpacked generated package; a non-zero exit fails the check. They are
    # commands rather than integrations because the scanner an operator trusts
    # is theirs to choose, and inventing a vendor API was not an option (§4).
    generation_security_scan_command: str = ""
    generation_vulnerability_scan_command: str = ""
    # The generated package ships its own offline pytest suite. Running it is
    # part of validation where pytest is importable.
    generation_run_generated_tests: bool = True

    # HashiCorp Vault as the secrets backend (LLD §4.3).
    # NOT TESTED against a live Vault: every request is built against the
    # documented KV v2 API and asserted against a stub transport.
    vault_addr: str = ""
    vault_token: str = ""
    vault_mount: str = "secret"
    vault_path_prefix: str = "sutr"
    vault_verify_tls: bool = True
    vault_timeout_seconds: float = 10.0

    # ── Failure handling (LLD §5.8) ──────────────────────────────────────────
    #
    # One number is not a timeout policy. A connect that has not completed in
    # a few seconds is not going to; a read that takes twenty is often a
    # provider doing real work. Separating them makes the fast failure fast,
    # which is what lets the circuit breaker notice a dead host quickly.
    provider_timeout_seconds: float = 30.0
    provider_connect_timeout_seconds: float = 5.0
    provider_read_timeout_seconds: float = 30.0
    # Waiting for a free connection is a *local* queue: a long wait means this
    # process is saturated, not that the provider is slow.
    provider_pool_timeout_seconds: float = 5.0

    # Retries. Deliberately few, and only for transport failures on safe
    # methods — a response that arrived, of any status, reached the provider's
    # application, and this platform does not know whether the tool it called
    # was idempotent.
    provider_retry_attempts: int = 2
    provider_retry_base_seconds: float = 0.2
    provider_retry_max_seconds: float = 5.0

    # The circuit breaker. After this many consecutive provider failures, calls
    # fail immediately for the cool-off rather than each occupying a worker for
    # the full timeout. State is per process (see platform/scaling.py).
    circuit_breaker_enabled: bool = True
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_cooloff_seconds: float = 30.0

    # ── Running more than one of these (LLD §5.4, §5.7) ──────────────────────
    #
    # "active" serves everything. "standby" serves reads and refuses writes
    # with a named 503 — the mode a region runs in when its database is a
    # read-only replica of another region's. Anything unrecognised means
    # active: a typo must not turn a region that serves writes into one that
    # refuses them.
    platform_mode: str = "active"
    # Which region owns the writes. Recorded, not discovered — this platform
    # does not elect a primary, and a guess would be worse than a null.
    primary_region: str = ""

    # Connection pooling. These matter as soon as there is more than one
    # replica: N replicas times pool_size is the number of server connections
    # PostgreSQL is asked for, and exceeding max_connections takes the whole
    # platform down rather than one replica. Ignored for SQLite.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800
    # A ceiling on any single statement, applied by the server. Without one, a
    # query that will never finish holds a pooled connection forever, and the
    # replica stops serving long before anyone notices the query.
    db_statement_timeout_ms: int = 0

    # Which region this instance runs in. LLD §5.1.2 lists region among the
    # dimensions a usage event carries, and §5.3's log field set names it too.
    # Empty means unset, and an unset region is recorded as null rather than as
    # a guess.
    region: str = ""

    # Revenue share (LLD §5.1, build prompt §44).
    # Basis points to the provider: 8000 is the LLD's 80/20 example. Read from
    # configuration on every settlement rather than captured at import, and
    # recorded on the settlement row — the percentage must never be hard-coded,
    # and an old settlement is explained by the share that applied then.
    revenue_share_provider_bps: int = 8000
    # How long the dunning workflow waits between attempts, and how many it
    # makes before giving up and leaving the invoice unpaid for a human.
    dunning_retry_hours: int = 72
    dunning_max_attempts: int = 4

    # Scoped access passes (LLD §4.3).
    # Left empty, the signing key is derived from JWT_SECRET_KEY by HKDF with a
    # distinct info string, so a pass and a session token are cryptographically
    # separated even though one secret is configured. Set this to control the
    # pass key directly — for instance to rotate it without invalidating every
    # user session.
    access_pass_secret: str = ""

    # Discovery (LLD §3.8).
    # The LLD's target for a discovery search is under 500 ms. This is not a
    # claim that it meets it — it is the point at which a request stops
    # spending time on ranking and falls back to retrieval order, which is a
    # decision the code makes and a test can check.
    discovery_budget_ms: int = 500

    # Kubernetes runtime (LLD §4.3.8, §5.7).
    kubernetes_enabled: bool = False
    # Left empty in-cluster: the pod's own service-account token and CA are
    # used, which is the documented in-cluster configuration.
    kubernetes_api_server: str = ""
    kubernetes_token: str = ""
    kubernetes_ca_cert_path: str = ""
    # The base image the generated package is mounted into. There is no image
    # build step in this provider, so without an image there is nothing to run.
    kubernetes_runtime_image: str = ""
    kubernetes_namespace_prefix: str = "sutr"
    kubernetes_verify_tls: bool = True
    kubernetes_request_timeout_seconds: float = 30.0

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
