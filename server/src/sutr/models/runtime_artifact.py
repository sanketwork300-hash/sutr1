import uuid
from datetime import datetime, timezone

from sqlmodel import Field, LargeBinary, SQLModel, UniqueConstraint

# Validation outcome. An artifact is either fit to deploy or it is not; there
# is no third state, because "deployable with reservations" is a decision the
# platform would be making on the operator's behalf.
STATUS_VALIDATED = "validated"
STATUS_REJECTED = "rejected"

RUNTIME_PYTHON = "python"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeArtifact(SQLModel, table=True):
    """One immutable, versioned MCP runtime build (LLD §3.6).

    The LLD's requirement is *"every stage produces immutable artifacts ·
    identical inputs ⇒ identical outputs"*. Both halves are enforced here:

    - **Immutable.** Nothing updates this row after it is written. There is no
      PATCH route, and the package, SBOM, manifest and validation report are
      stored on the row rather than re-derived on read, so what a reader sees
      is what was actually built and actually validated.
    - **Identical inputs ⇒ identical outputs.** `build_hash` is computed from
      the generation inputs and the bytes produced. Generating twice from an
      unchanged IR returns the existing row instead of writing a second one,
      which is how the determinism claim stays checkable rather than asserted:
      a duplicate row would mean the build was not deterministic.

    `knowledge_hash` covers the documentation knowledge folded into the build.
    It is separate from `ir_hash` because the same API can be regenerated after
    its documentation changes, and that is a different artifact even though the
    specification did not move.
    """

    __tablename__ = "runtime_artifact"
    __table_args__ = (
        # Per tenant, because two orgs generating from the same public spec
        # produce the same bytes and must still own separate artifacts.
        UniqueConstraint("org_id", "build_hash", name="uq_runtime_artifact_build"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    org_id: uuid.UUID = Field(foreign_key="org.id", index=True)
    project_id: uuid.UUID | None = Field(default=None, foreign_key="openapi_project.id", index=True)
    name: str
    slug: str = Field(index=True)

    # Which template produced the package, and which version of it. Versioned
    # independently of the IR, as the LLD requires: a template fix is a new
    # artifact even when the API has not changed.
    runtime: str = Field(default=RUNTIME_PYTHON)
    template_version: str = ""

    ir_version: int | None = None
    ir_hash: str = ""
    knowledge_hash: str = ""
    build_hash: str = Field(index=True)

    package_zip: bytes = Field(sa_type=LargeBinary)
    package_sha256: str = ""
    package_bytes: int = 0
    tool_count: int = 0

    manifest_json: str = Field(default="{}")
    sbom_json: str = Field(default="{}")
    validation_json: str = Field(default="{}")
    knowledge_json: str = Field(default="{}")

    status: str = Field(default=STATUS_REJECTED, index=True)

    # A detached signature over `build_hash`. Empty when no signing key is
    # configured — an unsigned artifact says so rather than carrying a
    # placeholder that would read as a signature.
    signature: str = ""
    signature_algorithm: str = ""
    signature_key_id: str = ""

    created_by_user_id: uuid.UUID | None = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
