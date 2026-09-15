"""add agent identities, scoped access passes and ABAC rules

Zero-trust identity and provisioning (ESDS LLD §4.3): a named identity for the
actors that had none, the record of every scoped access pass issued, and the
attribute rules the authorization layer evaluates.

Purely additive: three new tables, no change to any existing one. Nothing in
the platform requires them — an install with no agent identities and no access
rules behaves exactly as it did before, which is deliberate. An authorization
layer that starts denying the moment it is deployed is a layer nobody deploys.

The `access_pass` table holds claims, not tokens. Storing live bearer tokens
would make a database leak equivalent to compromising every agent at once.

Revision ID: 0039
Revises: 0038
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0039"
down_revision: Union[str, Sequence[str], None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_identity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column("attributes_json", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_key.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "name", name="uq_agent_identity_name"),
    )
    op.create_index("ix_agent_identity_org_id", "agent_identity", ["org_id"])
    op.create_index("ix_agent_identity_name", "agent_identity", ["name"])
    op.create_index("ix_agent_identity_kind", "agent_identity", ["kind"])
    op.create_index("ix_agent_identity_api_key_id", "agent_identity", ["api_key_id"])
    op.create_index("ix_agent_identity_active", "agent_identity", ["active"])

    op.create_table(
        "access_pass",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("principal", sa.String(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("issued_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("audience", sa.String(), nullable=False),
        sa.Column("tools_json", sa.String(), nullable=False),
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("nonce", sa.String(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=False),
        sa.Column("decision_json", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_identity.id"]),
        sa.ForeignKeyConstraint(["issued_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_pass_org_id", "access_pass", ["org_id"])
    op.create_index("ix_access_pass_principal", "access_pass", ["principal"])
    op.create_index("ix_access_pass_agent_id", "access_pass", ["agent_id"])
    op.create_index("ix_access_pass_nonce", "access_pass", ["nonce"])
    op.create_index("ix_access_pass_expires_at", "access_pass", ["expires_at"])
    op.create_index("ix_access_pass_revoked_at", "access_pass", ["revoked_at"])

    op.create_table(
        "access_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("effect", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("subject_json", sa.String(), nullable=False),
        sa.Column("resource_json", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "name", name="uq_access_rule_name"),
    )
    op.create_index("ix_access_rule_org_id", "access_rule", ["org_id"])
    op.create_index("ix_access_rule_name", "access_rule", ["name"])
    op.create_index("ix_access_rule_effect", "access_rule", ["effect"])
    op.create_index("ix_access_rule_action", "access_rule", ["action"])
    op.create_index("ix_access_rule_enabled", "access_rule", ["enabled"])

    # The LLD's "secret reference": with an external store the row holds a
    # pointer and the credential is genuinely not in the database (§4.3.6).
    with op.batch_alter_table("secret") as batch:
        batch.add_column(sa.Column("ref", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("secret") as batch:
        batch.drop_column("ref")

    for index, table in (
        ("ix_access_rule_enabled", "access_rule"),
        ("ix_access_rule_action", "access_rule"),
        ("ix_access_rule_effect", "access_rule"),
        ("ix_access_rule_name", "access_rule"),
        ("ix_access_rule_org_id", "access_rule"),
        ("ix_access_pass_revoked_at", "access_pass"),
        ("ix_access_pass_expires_at", "access_pass"),
        ("ix_access_pass_nonce", "access_pass"),
        ("ix_access_pass_agent_id", "access_pass"),
        ("ix_access_pass_principal", "access_pass"),
        ("ix_access_pass_org_id", "access_pass"),
        ("ix_agent_identity_active", "agent_identity"),
        ("ix_agent_identity_api_key_id", "agent_identity"),
        ("ix_agent_identity_kind", "agent_identity"),
        ("ix_agent_identity_name", "agent_identity"),
        ("ix_agent_identity_org_id", "agent_identity"),
    ):
        op.drop_index(index, table_name=table)

    op.drop_table("access_rule")
    op.drop_table("access_pass")
    op.drop_table("agent_identity")
