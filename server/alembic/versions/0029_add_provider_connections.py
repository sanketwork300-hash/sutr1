"""add provider connections and cloud deployment placement

Connected accounts (GitHub for spec sources; Google Cloud, Azure, and AWS for
deployment targets) plus the two columns a deployment needs to address a cloud
target again later: which connection authorized it, and where it was placed.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-21

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op  # noqa: E402

revision: str = "0029"
down_revision: Union[str, Sequence[str], None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_connection",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("account_label", sa.String(), nullable=False, server_default=""),
        sa.Column("scopes", sa.String(), nullable=False, server_default=""),
        sa.Column("access_secret_id", sa.Uuid(), nullable=True),
        sa.Column("refresh_secret_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("metadata_json", sa.String(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["access_secret_id"], ["secret.id"]),
        sa.ForeignKeyConstraint(["refresh_secret_id"], ["secret.id"]),
    )
    op.create_index("ix_provider_connection_org_id", "provider_connection", ["org_id"])
    op.create_index("ix_provider_connection_user_id", "provider_connection", ["user_id"])
    op.create_index("ix_provider_connection_provider", "provider_connection", ["provider"])
    # One connection per identity per provider: re-authorizing replaces the
    # row rather than accumulating tokens nobody can tell apart.
    op.create_index(
        "ix_provider_connection_unique",
        "provider_connection",
        ["org_id", "user_id", "provider"],
        unique=True,
    )

    op.create_table(
        "oauth_connect_state",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_verifier", sa.String(), nullable=False, server_default=""),
        sa.Column("device_payload_json", sa.String(), nullable=False, server_default="{}"),
        sa.Column("redirect_after", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
    )
    op.create_index("ix_oauth_connect_state_state", "oauth_connect_state", ["state"], unique=True)
    op.create_index("ix_oauth_connect_state_provider", "oauth_connect_state", ["provider"])

    with op.batch_alter_table("deployment") as batch:
        batch.add_column(sa.Column("connection_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("config_json", sa.String(), nullable=False, server_default="{}"))
        batch.create_foreign_key(
            "fk_deployment_connection_id", "provider_connection", ["connection_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("deployment") as batch:
        batch.drop_constraint("fk_deployment_connection_id", type_="foreignkey")
        batch.drop_column("config_json")
        batch.drop_column("connection_id")

    op.drop_index("ix_oauth_connect_state_provider", table_name="oauth_connect_state")
    op.drop_index("ix_oauth_connect_state_state", table_name="oauth_connect_state")
    op.drop_table("oauth_connect_state")

    op.drop_index("ix_provider_connection_unique", table_name="provider_connection")
    op.drop_index("ix_provider_connection_provider", table_name="provider_connection")
    op.drop_index("ix_provider_connection_user_id", table_name="provider_connection")
    op.drop_index("ix_provider_connection_org_id", table_name="provider_connection")
    op.drop_table("provider_connection")
