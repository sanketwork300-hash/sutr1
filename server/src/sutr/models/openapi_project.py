import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OpenAPIProject(SQLModel, table=True):
    """An imported OpenAPI specification and its compilation state.

    The raw spec text is retained (bounded by the import size limit) so the
    project can be re-compiled with different filters without re-uploading.
    Compilation materializes/updates a CustomApiIntegration row, which is what
    the rest of the platform (tools, approvals, playground, MCP) consumes.
    """

    __tablename__ = "openapi_project"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    workspace_id: uuid.UUID | None = Field(default=None, foreign_key="workspace.id")
    name: str
    source_kind: str = Field(default="paste")  # paste | upload | url
    source_url: str | None = Field(default=None)
    spec_text: str  # raw document as imported (JSON or YAML)
    # Serialized ApiDefinition IR (JSON) produced at import time.
    ir_json: str
    warnings_json: str = Field(default="[]")
    # Last-used compile inputs (JSON), for re-compiles and UI prefill.
    filters_json: str = Field(default="{}")
    server_url: str | None = Field(default=None)
    server_variables_json: str = Field(default="{}")
    # Link to the integration produced by the last successful compile.
    integration_db_id: uuid.UUID | None = Field(
        default=None, foreign_key="custom_api_integration.id"
    )
    status: str = Field(default="imported")  # imported | compiled
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
