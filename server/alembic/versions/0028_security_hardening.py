"""drop value-derived secret columns; add processed_stripe_event

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-20

secret.value_hash (unsalted SHA-256 of the secret) and secret.prefix (its first
12 plaintext characters) were written but never read, and both leaked material
about the secret even when the KMS backend was in use. They are dropped rather
than kept "just in case".

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op  # noqa: E402

revision: str = "0028"
down_revision: Union[str, Sequence[str], None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table so SQLite (which cannot DROP COLUMN in place on older
    # versions) is handled by table rebuild.
    with op.batch_alter_table("secret") as batch:
        batch.drop_index("ix_secret_value_hash")
        batch.drop_column("value_hash")
        batch.drop_column("prefix")

    op.create_table(
        "processed_stripe_event",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("event_type", sa.String(), nullable=False, server_default=""),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processed_stripe_event")
    with op.batch_alter_table("secret") as batch:
        # Restored empty: the original values are unrecoverable by design.
        batch.add_column(sa.Column("value_hash", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("prefix", sa.String(), nullable=True))
        batch.create_index("ix_secret_value_hash", ["value_hash"])
