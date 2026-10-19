"""security fixes: API key roles, TOTP hardening, Google login nonce

Three findings from the security audit, one migration:

- API keys carry the role of the member who minted them (`api_key.role`) and
  are checked through the same permission matrix as a user session. Existing
  keys get "developer": the documented key capabilities (install + call)
  without the owner/admin-only surfaces they previously reached by accident.
- The TOTP seed moves into the secrets backend (`user.totp_secret_id` ->
  secret.id); the legacy plaintext column stays readable and is migrated the
  next time each user verifies a code. `user.totp_last_used_step` records the
  time step that last authenticated the user so a code cannot be replayed.
- Google sign-in binds each flow to a browser-generated nonce
  (`google_login_state.nonce`) that is echoed back in the callback fragment.

Revision ID: 0044
Revises: 0043
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: Union[str, Sequence[str], None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_key",
        sa.Column("role", sa.String(), nullable=False, server_default="developer"),
    )
    op.add_column(
        "google_login_state",
        sa.Column("nonce", sa.String(), nullable=True),
    )
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("totp_secret_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("totp_last_used_step", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "user_totp_secret_id_fkey", "secret", ["totp_secret_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_constraint("user_totp_secret_id_fkey", type_="foreignkey")
        batch_op.drop_column("totp_last_used_step")
        batch_op.drop_column("totp_secret_id")
    op.drop_column("google_login_state", "nonce")
    op.drop_column("api_key", "role")
