"""one replica at a time for the work that must happen once

Running the API on several replicas is the point of scaling out. Running the
background loops on several replicas is a bug: the deployment sweep meters
runtime minutes, so two replicas sweeping the same five minutes bill a tenant
for ten. Nothing raises, nothing logs, and the number is simply wrong.

This table is the lease that stops it. One row per job, held by one replica,
renewed while it works and expiring on its own when that replica dies. It is a
lease rather than a database lock because a lease behaves the same on SQLite
and Postgres, survives a connection pooler in transaction mode — where a
session-level advisory lock does not — and can be read with a SELECT when
somebody asks which replica is sweeping.

Revision ID: 0043
Revises: 0042
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0043"
down_revision: Union[str, Sequence[str], None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "leader_lease",
        # The job name is the primary key: two rows for one job is precisely
        # the state this table exists to make impossible.
        sa.Column("job", sa.String(), primary_key=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("renewed_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("id", sa.Uuid(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("leader_lease")
