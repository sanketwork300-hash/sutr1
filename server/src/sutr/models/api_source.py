import uuid
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

# What a source is for. LLD §3.3's two-phase model: one source bootstraps the
# project, and additional ones are attached to compare against it.
ROLE_PRIMARY = "primary"  # the source of truth this project is generated from
ROLE_COMPARISON = "comparison"  # watched only to report drift against the primary

ROLES = (ROLE_PRIMARY, ROLE_COMPARISON)

# What may happen automatically when a watched source changes. Build prompt
# §13: "Breaking changes must not automatically deploy unless policy permits."
APPLY_NEVER = "never"  # record the drift; a human decides
APPLY_NON_BREAKING = "non_breaking"  # re-import automatically unless something breaks
APPLY_ALWAYS = "always"  # re-import whatever changed

APPLY_POLICIES = (APPLY_NEVER, APPLY_NON_BREAKING, APPLY_ALWAYS)


class ApiSource(SQLModel, table=True):
    """One place a project's definition is read from.

    A project has one primary source and any number of comparison sources. The
    LLD's example is a team whose source of truth is GitHub while an API
    gateway also serves a definition: watching both and reporting the
    difference is what turns MCP generation into API governance (§3.3).

    Provenance is stored per source rather than per project because two sources
    disagree about what "current" means — a commit SHA and an ETag are not
    comparable, and each source's answer belongs next to it.
    """

    __tablename__ = "api_source"
    __table_args__ = (
        # One source per (project, connector, uri): re-adding the same source
        # should update it rather than accumulate duplicates that all poll.
        UniqueConstraint("project_id", "connector", "source_uri", name="uq_api_source_locator"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    project_id: uuid.UUID = Field(foreign_key="openapi_project.id", index=True)
    connector: str = Field(index=True)
    role: str = Field(default=ROLE_PRIMARY)
    label: str = ""

    # What the connector needs to fetch: URL, path, branch, and so on. Never
    # credentials — those are secret references below.
    config_json: str = Field(default="{}")
    # Secret rows holding a token or API key for this source.
    token_secret_id: uuid.UUID | None = Field(default=None, foreign_key="secret.id")
    # A connected account, when the source authenticates through one.
    connection_id: uuid.UUID | None = Field(default=None, foreign_key="provider_connection.id")

    # ── Provenance (build prompt §13) ────────────────────────────────────────
    source_uri: str = ""
    source_version: str | None = None
    commit_sha: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    retrieved_at: datetime | None = None
    # Hash of the raw document, and of the IR derived from it. Both, because a
    # file can change without the API changing.
    content_hash: str | None = None
    ir_hash: str | None = None
    # The IR this source last produced, kept so drift is a comparison rather
    # than a re-fetch of the other side.
    ir_json: str | None = None

    # ── Continuous sync (LLD §3.3) ───────────────────────────────────────────
    watch_enabled: bool = Field(default=False)
    watch_interval_seconds: int = Field(default=3600)
    apply_policy: str = Field(default=APPLY_NEVER)
    last_checked_at: datetime | None = None
    last_changed_at: datetime | None = None
    # Consecutive check failures. A source that has been failing for a while
    # is backed off rather than polled at full rate forever.
    consecutive_failures: int = Field(default=0)
    last_error: str | None = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
