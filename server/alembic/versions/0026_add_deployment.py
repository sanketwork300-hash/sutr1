"""add deployment

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-19

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op  # noqa: E402

revision: str = "0026"
down_revision: Union[str, Sequence[str], None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deployment",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("provider_state_json", sa.String(), nullable=False, server_default="{}"),
        sa.Column("package_zip", sa.LargeBinary(), nullable=False),
        sa.Column("tool_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("env_var", sa.String(), nullable=True),
        sa.Column("token_secret_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["openapi_project.id"]),
        sa.ForeignKeyConstraint(["token_secret_id"], ["secret.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
    )
    op.create_index("ix_deployment_org_id", "deployment", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_deployment_org_id", table_name="deployment")
    op.drop_table("deployment")
