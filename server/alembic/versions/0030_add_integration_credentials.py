"""add integration_credential

One credential per security scheme, replacing the single-header-per-integration
assumption (ADR-009 / build prompt §24). Purely additive: the existing
`custom_api_integration.token_header` / `token_format` columns stay and remain
the credential used when no per-scheme row exists, so every integration
configured before this migration keeps working untouched.

Revision ID: 0030
Revises: 0029
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
down_revision: Union[str, Sequence[str], None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "integration_credential",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.String(), nullable=False),
        sa.Column("scheme_name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=False),
        sa.Column("param_name", sa.String(), nullable=False),
        sa.Column("value_format", sa.String(), nullable=False),
        sa.Column("secret_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.String(), nullable=True),
        sa.Column("client_secret_id", sa.Uuid(), nullable=True),
        sa.Column("token_url", sa.String(), nullable=True),
        sa.Column("scopes", sa.String(), nullable=False),
        sa.Column("access_token_secret_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["secret_id"], ["secret.id"]),
        sa.ForeignKeyConstraint(["client_secret_id"], ["secret.id"]),
        sa.ForeignKeyConstraint(["access_token_secret_id"], ["secret.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "integration_id", "scheme_name", name="uq_integration_credential_scheme"
        ),
    )
    op.create_index(op.f("ix_integration_credential_org_id"), "integration_credential", ["org_id"])
    op.create_index(
        op.f("ix_integration_credential_integration_id"),
        "integration_credential",
        ["integration_id"],
    )
    # The translated security schemes, so the console can ask for exactly the
    # credentials this API declares without re-reading the specification.
    op.add_column(
        "custom_api_integration",
        sa.Column("auth_json", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("custom_api_integration", "auth_json")
    op.drop_index(
        op.f("ix_integration_credential_integration_id"), table_name="integration_credential"
    )
    op.drop_index(op.f("ix_integration_credential_org_id"), table_name="integration_credential")
    op.drop_table("integration_credential")
