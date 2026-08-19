from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context
from sutr.config import settings
from sutr.models.api_key import ApiKey  # noqa: F401
from sutr.models.audit_event import AuditEvent  # noqa: F401
from sutr.models.custom_api_integration import CustomApiIntegration  # noqa: F401
from sutr.models.custom_mcp_integration import CustomMcpIntegration  # noqa: F401
from sutr.models.deployment import Deployment  # noqa: F401
from sutr.models.google_login_state import GoogleLoginState  # noqa: F401
from sutr.models.instance_settings import InstanceSettings  # noqa: F401
from sutr.models.integration import InstalledIntegration  # noqa: F401
from sutr.models.log import LogEntry  # noqa: F401
from sutr.models.oauth import OAuthState  # noqa: F401
from sutr.models.oauth_auth_code import OAuthAuthCode  # noqa: F401
from sutr.models.oauth_auth_request import OAuthAuthRequest  # noqa: F401
from sutr.models.oauth_client import OAuthClient  # noqa: F401
from sutr.models.oauth_revoked_token import OAuthRevokedToken  # noqa: F401
from sutr.models.openapi_project import OpenAPIProject  # noqa: F401
from sutr.models.org import Org  # noqa: F401
from sutr.models.org_invitation import OrgInvitation  # noqa: F401
from sutr.models.org_membership import OrgMembership  # noqa: F401
from sutr.models.secret import Secret  # noqa: F401
from sutr.models.subscription import Subscription  # noqa: F401
from sutr.models.tool_approval_request import ToolApprovalRequest  # noqa: F401
from sutr.models.tool_cache import ToolCache  # noqa: F401
from sutr.models.tool_execution import ToolExecutionSetting  # noqa: F401
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
