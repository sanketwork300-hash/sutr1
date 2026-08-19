"""add openapi_project

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-19

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op  # noqa: E402

revision: str = "0024"
down_revision: Union[str, Sequence[str], None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "openapi_project",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("source_kind", sa.String(), nullable=False, server_default="paste"),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.Column("spec_text", sa.String(), nullable=False),
        sa.Column("ir_json", sa.String(), nullable=False),
        sa.Column("warnings_json", sa.String(), nullable=False, server_default="[]"),
        sa.Column("filters_json", sa.String(), nullable=False, server_default="{}"),
        sa.Column("server_url", sa.String(), nullable=True),
        sa.Column("server_variables_json", sa.String(), nullable=False, server_default="{}"),
        sa.Column("integration_db_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="imported"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"]),
        sa.ForeignKeyConstraint(["integration_db_id"], ["custom_api_integration.id"]),
    )
    op.create_index("ix_openapi_project_org_id", "openapi_project", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_openapi_project_org_id", table_name="openapi_project")
    op.drop_table("openapi_project")
