"""add workspaces, org invitations, membership timestamps, token versioning

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-19

"""

import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op  # noqa: E402

revision: str = "0023"
down_revision: Union[str, Sequence[str], None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspace",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.UniqueConstraint("org_id", "slug", name="uq_workspace_org_slug"),
    )
    op.create_index("ix_workspace_org_id", "workspace", ["org_id"])

    op.create_table(
        "org_invitation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="member"),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("invited_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("accepted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["accepted_by_user_id"], ["user.id"]),
    )
    op.create_index("ix_org_invitation_org_id", "org_invitation", ["org_id"])
    op.create_index("ix_org_invitation_email", "org_invitation", ["email"])
    op.create_index("ix_org_invitation_token_hash", "org_invitation", ["token_hash"], unique=True)

    op.add_column("org_membership", sa.Column("created_at", sa.DateTime(), nullable=True))
    op.add_column(
        "user",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )

    # Every existing org gets a default workspace. Typed table constructs so
    # the Uuid columns round-trip correctly on both SQLite and Postgres.
    conn = op.get_bind()
    org_table = sa.table("org", sa.column("id", sa.Uuid()))
    workspace_table = sa.table(
        "workspace",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("slug", sa.String()),
        sa.column("is_default", sa.Boolean()),
        sa.column("created_at", sa.DateTime()),
    )
    now = datetime.now(timezone.utc)
    for org_id in conn.execute(sa.select(org_table.c.id)).scalars():
        conn.execute(
            sa.insert(workspace_table).values(
                id=uuid.uuid4(),
                org_id=org_id,
                name="Default",
                slug="default",
                is_default=True,
                created_at=now,
            )
        )


def downgrade() -> None:
    op.drop_column("user", "token_version")
    op.drop_column("org_membership", "created_at")
    op.drop_index("ix_org_invitation_token_hash", table_name="org_invitation")
    op.drop_index("ix_org_invitation_email", table_name="org_invitation")
    op.drop_index("ix_org_invitation_org_id", table_name="org_invitation")
    op.drop_table("org_invitation")
    op.drop_index("ix_workspace_org_id", table_name="workspace")
    op.drop_table("workspace")
