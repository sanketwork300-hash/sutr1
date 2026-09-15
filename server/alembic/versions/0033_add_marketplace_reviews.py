"""add marketplace_review

Ratings and reviews for the marketplace (build prompt §42). Additive: an
integration with no reviews reports `rating: null` and `review_count: 0`,
which reads as "nobody has rated this" rather than as a bad score.

Revision ID: 0033
Revises: 0032
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0033"
down_revision: Union[str, Sequence[str], None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "marketplace_review",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.String(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("body", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "user_id", "integration_id", name="uq_marketplace_review_author"
        ),
    )
    op.create_index(op.f("ix_marketplace_review_org_id"), "marketplace_review", ["org_id"])
    op.create_index(op.f("ix_marketplace_review_user_id"), "marketplace_review", ["user_id"])
    op.create_index(
        op.f("ix_marketplace_review_integration_id"), "marketplace_review", ["integration_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_marketplace_review_integration_id"), table_name="marketplace_review")
    op.drop_index(op.f("ix_marketplace_review_user_id"), table_name="marketplace_review")
    op.drop_index(op.f("ix_marketplace_review_org_id"), table_name="marketplace_review")
    op.drop_table("marketplace_review")
