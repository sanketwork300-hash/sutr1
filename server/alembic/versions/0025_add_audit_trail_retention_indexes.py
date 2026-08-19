"""add audit_event, org.log_retention_days, log_entry filter indexes

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-19

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op  # noqa: E402

revision: str = "0025"
down_revision: Union[str, Sequence[str], None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("actor_type", sa.String(), nullable=False, server_default="user"),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_api_key_prefix", sa.String(), nullable=True),
        sa.Column("impersonator_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_type", sa.String(), nullable=True),
        sa.Column("target_id", sa.String(), nullable=True),
        sa.Column("summary", sa.String(), nullable=False, server_default=""),
        sa.Column("metadata_json", sa.String(), nullable=False, server_default="{}"),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["impersonator_user_id"], ["user.id"]),
    )
    op.create_index("ix_audit_event_org_id", "audit_event", ["org_id"])
    op.create_index("ix_audit_event_org_action", "audit_event", ["org_id", "action"])

    with op.batch_alter_table("org") as batch:
        batch.add_column(sa.Column("log_retention_days", sa.Integer(), nullable=True))

    op.create_index("ix_log_entry_org_integration", "log_entry", ["org_id", "integration_id"])
    op.create_index("ix_log_entry_org_tool", "log_entry", ["org_id", "tool_name"])
    op.create_index("ix_log_entry_org_outcome", "log_entry", ["org_id", "outcome"])
    op.create_index("ix_log_entry_org_timestamp", "log_entry", ["org_id", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_log_entry_org_timestamp", table_name="log_entry")
    op.drop_index("ix_log_entry_org_outcome", table_name="log_entry")
    op.drop_index("ix_log_entry_org_tool", table_name="log_entry")
    op.drop_index("ix_log_entry_org_integration", table_name="log_entry")
    with op.batch_alter_table("org") as batch:
        batch.drop_column("log_retention_days")
    op.drop_index("ix_audit_event_org_action", table_name="audit_event")
    op.drop_index("ix_audit_event_org_id", table_name="audit_event")
    op.drop_table("audit_event")
