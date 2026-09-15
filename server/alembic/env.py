from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context
from sutr.config import settings
from sutr.models.access_pass import AccessPass  # noqa: F401
from sutr.models.access_rule import AccessRule  # noqa: F401
from sutr.models.agent_identity import AgentIdentity  # noqa: F401
from sutr.models.api_key import ApiKey  # noqa: F401
from sutr.models.api_source import ApiSource  # noqa: F401
from sutr.models.audit_event import AuditEvent  # noqa: F401
from sutr.models.business_rule import BusinessRule  # noqa: F401
from sutr.models.chunk_embedding import ChunkEmbedding  # noqa: F401
from sutr.models.consumed_event import ConsumedEvent  # noqa: F401
from sutr.models.custom_api_integration import CustomApiIntegration  # noqa: F401
from sutr.models.custom_mcp_integration import CustomMcpIntegration  # noqa: F401
from sutr.models.deployment import Deployment  # noqa: F401
from sutr.models.deployment_revision import DeploymentRevision  # noqa: F401
from sutr.models.doc_workflow import DocWorkflow, GlossaryTerm  # noqa: F401
from sutr.models.document import Document  # noqa: F401
from sutr.models.document_chunk import DocumentChunk  # noqa: F401
from sutr.models.document_job import DocumentJob  # noqa: F401
from sutr.models.drift_report import DriftReport  # noqa: F401
from sutr.models.google_login_state import GoogleLoginState  # noqa: F401
from sutr.models.governance_exception import GovernanceException  # noqa: F401
from sutr.models.governance_policy import (  # noqa: F401
    GovernancePolicy,
    GovernancePolicyVersion,
)
from sutr.models.governance_review import GovernanceReview  # noqa: F401
from sutr.models.governance_run import ComplianceRun, RiskAssessment  # noqa: F401
from sutr.models.idempotency_key import IdempotencyKey  # noqa: F401
from sutr.models.instance_settings import InstanceSettings  # noqa: F401
from sutr.models.integration import InstalledIntegration  # noqa: F401
from sutr.models.integration_credential import IntegrationCredential  # noqa: F401
from sutr.models.invoice import Invoice, InvoiceLine  # noqa: F401
from sutr.models.knowledge_graph import KnowledgeEdge, KnowledgeNode  # noqa: F401
from sutr.models.ledger_entry import LedgerEntry  # noqa: F401
from sutr.models.log import LogEntry  # noqa: F401
from sutr.models.marketplace_listing import MarketplaceListing  # noqa: F401
from sutr.models.marketplace_review import MarketplaceReview  # noqa: F401
from sutr.models.oauth import OAuthState  # noqa: F401
from sutr.models.oauth_auth_code import OAuthAuthCode  # noqa: F401
from sutr.models.oauth_auth_request import OAuthAuthRequest  # noqa: F401
from sutr.models.oauth_client import OAuthClient  # noqa: F401
from sutr.models.oauth_revoked_token import OAuthRevokedToken  # noqa: F401
from sutr.models.openapi_project import OpenAPIProject  # noqa: F401
from sutr.models.org import Org  # noqa: F401
from sutr.models.org_invitation import OrgInvitation  # noqa: F401
from sutr.models.org_membership import OrgMembership  # noqa: F401
from sutr.models.outbox_event import OutboxEvent  # noqa: F401
from sutr.models.pricing_plan import PricingPlan  # noqa: F401
from sutr.models.processed_stripe_event import ProcessedStripeEvent  # noqa: F401
from sutr.models.provider_profile import ProviderProfile  # noqa: F401
from sutr.models.quota import Quota  # noqa: F401
from sutr.models.registry_change_request import RegistryChangeRequest  # noqa: F401
from sutr.models.registry_pricing import RegistryPricing  # noqa: F401
from sutr.models.registry_tool import RegistryTool  # noqa: F401
from sutr.models.registry_version import RegistryVersion  # noqa: F401
from sutr.models.runtime_artifact import RuntimeArtifact  # noqa: F401
from sutr.models.secret import Secret  # noqa: F401
from sutr.models.settlement import PaymentAttempt, Settlement  # noqa: F401
from sutr.models.subscription import Subscription  # noqa: F401
from sutr.models.tool_approval_request import ToolApprovalRequest  # noqa: F401
from sutr.models.tool_cache import ToolCache  # noqa: F401
from sutr.models.tool_execution import ToolExecutionSetting  # noqa: F401
from sutr.models.tool_subscription import ToolSubscription  # noqa: F401
from sutr.models.usage_event import UsageEvent  # noqa: F401
from sutr.models.user import User  # noqa: F401
from sutr.models.waitlist import Waitlist  # noqa: F401
from sutr.models.workspace import Workspace  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
