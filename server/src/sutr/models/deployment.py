import uuid
from datetime import datetime

from sqlmodel import Field, LargeBinary, SQLModel


class Deployment(SQLModel, table=True):
    """A generated MCP server running (or intended to run) on a provider.

    The package zip is snapshotted at create time so the deployment is
    reproducible even if the source OpenAPI project changes or is deleted.
    `provider_state_json` is the provider's opaque handle dict (container id,
    URL, ...) — the schema never learns provider internals. `config_json` is
    the opposite direction: what the user chose *before* the deploy, kept so
    the same target can be addressed again later.
    """

    __tablename__ = "deployment"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="openapi_project.id")
    # The validated runtime artifact this deployment is running, when it was
    # created from one. Null for a deployment compiled straight from a project,
    # which is the path that predates the artifact store and still works.
    artifact_id: uuid.UUID | None = Field(default=None, foreign_key="runtime_artifact.id")
    name: str
    slug: str
    provider: str  # "docker" | "gcp" | "azure" | "aws"
    # Which connected account authorizes this deployment's provider. None for
    # the local Docker provider, which runs on the sutr host itself.
    connection_id: uuid.UUID | None = Field(default=None, foreign_key="provider_connection.id")
    # The user's placement choice (project/subscription/account, region, ...),
    # snapshotted so a later stop/start/delete does not depend on the builder
    # form still being open, or on the values still being defaults.
    config_json: str = Field(default="{}")
    # queued | building | running | stopped | failed
    status: str = Field(default="queued")
    url: str | None = None
    error: str | None = None
    provider_state_json: str = Field(default="{}")
    package_zip: bytes = Field(sa_type=LargeBinary)
    tool_count: int = Field(default=0)
    # Which revision is live. History lives in `deployment_revision`, and an
    # update or rollback moves this pointer rather than replacing the row.
    current_revision: int = Field(default=1)
    # Name of the env var the runtime token is injected as (None → no auth).
    env_var: str | None = None
    token_secret_id: uuid.UUID | None = Field(default=None, foreign_key="secret.id")
    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
