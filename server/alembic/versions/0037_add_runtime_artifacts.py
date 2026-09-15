"""add immutable MCP runtime artifacts

The MCP Generator's output (ESDS LLD §3.6): one row per generated runtime
build, holding the package bytes, the MCP manifest, the CycloneDX SBOM, the
validation report, the documentation knowledge folded into the build, and a
detached signature over the build hash.

Purely additive: one new table, no change to any existing one. The unique
constraint on (org_id, build_hash) is the mechanism behind the LLD's
determinism requirement — a rebuild from unchanged inputs finds the existing
row instead of writing a second one, so a duplicate would mean the build was
not deterministic.

Revision ID: 0037
Revises: 0036
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0037"
down_revision: Union[str, Sequence[str], None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("runtime", sa.String(), nullable=False),
        sa.Column("template_version", sa.String(), nullable=False),
        sa.Column("ir_version", sa.Integer(), nullable=True),
        sa.Column("ir_hash", sa.String(), nullable=False),
        sa.Column("knowledge_hash", sa.String(), nullable=False),
        sa.Column("build_hash", sa.String(), nullable=False),
        sa.Column("package_zip", sa.LargeBinary(), nullable=False),
        sa.Column("package_sha256", sa.String(), nullable=False),
        sa.Column("package_bytes", sa.Integer(), nullable=False),
        sa.Column("tool_count", sa.Integer(), nullable=False),
        sa.Column("manifest_json", sa.String(), nullable=False),
        sa.Column("sbom_json", sa.String(), nullable=False),
        sa.Column("validation_json", sa.String(), nullable=False),
        sa.Column("knowledge_json", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
        sa.Column("signature_algorithm", sa.String(), nullable=False),
        sa.Column("signature_key_id", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["openapi_project.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "build_hash", name="uq_runtime_artifact_build"),
    )
    op.create_index("ix_runtime_artifact_org_id", "runtime_artifact", ["org_id"])
    op.create_index("ix_runtime_artifact_project_id", "runtime_artifact", ["project_id"])
    op.create_index("ix_runtime_artifact_slug", "runtime_artifact", ["slug"])
    op.create_index("ix_runtime_artifact_build_hash", "runtime_artifact", ["build_hash"])
    op.create_index("ix_runtime_artifact_status", "runtime_artifact", ["status"])

    # Provenance: which validated artifact a deployment (and each of its
    # revisions) is running. Nullable, because deployments compiled straight
    # from a project predate the artifact store and keep working.
    with op.batch_alter_table("deployment") as batch:
        batch.add_column(sa.Column("artifact_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_deployment_artifact_id", "runtime_artifact", ["artifact_id"], ["id"]
        )
    with op.batch_alter_table("deployment_revision") as batch:
        batch.add_column(sa.Column("artifact_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_deployment_revision_artifact_id", "runtime_artifact", ["artifact_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("deployment_revision") as batch:
        batch.drop_constraint("fk_deployment_revision_artifact_id", type_="foreignkey")
        batch.drop_column("artifact_id")
    with op.batch_alter_table("deployment") as batch:
        batch.drop_constraint("fk_deployment_artifact_id", type_="foreignkey")
        batch.drop_column("artifact_id")
    op.drop_index("ix_runtime_artifact_status", table_name="runtime_artifact")
    op.drop_index("ix_runtime_artifact_build_hash", table_name="runtime_artifact")
    op.drop_index("ix_runtime_artifact_slug", table_name="runtime_artifact")
    op.drop_index("ix_runtime_artifact_project_id", table_name="runtime_artifact")
    op.drop_index("ix_runtime_artifact_org_id", table_name="runtime_artifact")
    op.drop_table("runtime_artifact")
