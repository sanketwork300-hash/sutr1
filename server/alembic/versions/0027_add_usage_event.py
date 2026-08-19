"""add usage_event metering ledger

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-19

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op  # noqa: E402

revision: str = "0027"
down_revision: Union[str, Sequence[str], None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usage_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("integration_id", sa.String(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("api_key_prefix", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.String(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
    )
    op.create_index("ix_usage_event_org_id", "usage_event", ["org_id"])
    op.create_index("ix_usage_event_org_ts", "usage_event", ["org_id", "timestamp"])
    op.create_index("ix_usage_event_org_kind_ts", "usage_event", ["org_id", "kind", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_usage_event_org_kind_ts", table_name="usage_event")
    op.drop_index("ix_usage_event_org_ts", table_name="usage_event")
    op.drop_index("ix_usage_event_org_id", table_name="usage_event")
    op.drop_table("usage_event")
