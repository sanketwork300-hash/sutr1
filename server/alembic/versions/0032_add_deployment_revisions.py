"""add deployment_revision

Versioned deployment artifacts with history and rollback (build prompt §36,
ADR-016). Additive: existing deployments keep working, and a revision row is
backfilled for each of them so their history starts at the version that is
actually running rather than at nothing.

Revision ID: 0032
Revises: 0031
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0032"
down_revision: Union[str, Sequence[str], None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deployment_revision",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("restored_from_revision", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("package_zip", sa.LargeBinary(), nullable=False),
        sa.Column("package_sha256", sa.String(), nullable=False),
        sa.Column("config_json", sa.String(), nullable=False),
        sa.Column("tool_count", sa.Integer(), nullable=False),
        sa.Column("provider_state_json", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployment.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "revision", name="uq_deployment_revision_number"),
    )
    op.create_index(
        op.f("ix_deployment_revision_deployment_id"), "deployment_revision", ["deployment_id"]
    )
    op.create_index(op.f("ix_deployment_revision_org_id"), "deployment_revision", ["org_id"])

    # Which revision is live. Existing rows are revision 1 by the backfill below.
    op.add_column(
        "deployment",
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="1"),
    )

    # Backfill: every existing deployment gets revision 1 recorded, so its
    # history begins at what is actually running rather than at nothing.
    op.execute(
        """
        INSERT INTO deployment_revision (
            id, deployment_id, org_id, revision, origin, restored_from_revision,
            outcome, error, package_zip, package_sha256, config_json, tool_count,
            provider_state_json, url, created_by_user_id, created_at
        )
        SELECT
            lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' ||
            substr(lower(hex(randomblob(2))), 2) || '-a' ||
            substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6))),
            d.id, d.org_id, 1, 'create', NULL,
            CASE WHEN d.status = 'failed' THEN 'failed' ELSE 'active' END,
            d.error, d.package_zip, '', d.config_json, d.tool_count,
            d.provider_state_json, d.url, d.created_by_user_id, d.created_at
        FROM deployment d
        """
        if op.get_bind().dialect.name == "sqlite"
        else """
        INSERT INTO deployment_revision (
            id, deployment_id, org_id, revision, origin, restored_from_revision,
            outcome, error, package_zip, package_sha256, config_json, tool_count,
            provider_state_json, url, created_by_user_id, created_at
        )
        SELECT
            gen_random_uuid(), d.id, d.org_id, 1, 'create', NULL,
            CASE WHEN d.status = 'failed' THEN 'failed' ELSE 'active' END,
            d.error, d.package_zip, '', d.config_json, d.tool_count,
            d.provider_state_json, d.url, d.created_by_user_id, d.created_at
        FROM deployment d
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("deployment") as batch:
        batch.drop_column("current_revision")
    op.drop_index(op.f("ix_deployment_revision_org_id"), table_name="deployment_revision")
    op.drop_index(op.f("ix_deployment_revision_deployment_id"), table_name="deployment_revision")
    op.drop_table("deployment_revision")
