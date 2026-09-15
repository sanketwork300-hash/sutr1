"""add quota

Usage limits evaluated before execution (ESDS LLD §5.1, build prompt §48).
Additive: with no quota rows configured, nothing is limited, so every existing
install behaves exactly as before.

Revision ID: 0031
Revises: 0030
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0031"
down_revision: Union[str, Sequence[str], None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "quota",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "kind", "scope", "scope_id", name="uq_quota_scope"),
    )
    op.create_index(op.f("ix_quota_org_id"), "quota", ["org_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_quota_org_id"), table_name="quota")
    op.drop_table("quota")
