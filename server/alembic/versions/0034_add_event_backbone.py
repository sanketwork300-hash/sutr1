"""add outbox_event, consumed_event, idempotency_key

The Phase 2 foundation (ESDS LLD §5.6, build prompt §76; ADR-003).

Additive and inert on an existing install: nothing writes to these tables until
a producer calls `events.publish` or a handler opts into idempotency, so every
existing route behaves exactly as before.

Revision ID: 0034
Revises: 0033
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0034"
down_revision: Union[str, Sequence[str], None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "outbox_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("partition_key", sa.String(), nullable=False),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("producer", sa.String(), nullable=False),
        sa.Column("envelope_json", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index(op.f("ix_outbox_event_event_id"), "outbox_event", ["event_id"], unique=True)
    op.create_index(op.f("ix_outbox_event_event_type"), "outbox_event", ["event_type"])
    op.create_index(op.f("ix_outbox_event_state"), "outbox_event", ["state"])
    op.create_index(op.f("ix_outbox_event_tenant_id"), "outbox_event", ["tenant_id"])
    # The relay's hot query: oldest pending first.
    op.create_index("ix_outbox_event_state_id", "outbox_event", ["state", "id"])
    op.create_index("ix_outbox_event_type_id", "outbox_event", ["event_type", "id"])

    op.create_table(
        "consumed_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("consumer", sa.String(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("consumer", "event_id", name="uq_consumed_event_consumer"),
    )
    op.create_index(op.f("ix_consumed_event_consumer"), "consumed_event", ["consumer"])
    op.create_index(op.f("ix_consumed_event_event_id"), "consumed_event", ["event_id"])

    op.create_table(
        "idempotency_key",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "endpoint", "key", name="uq_idempotency_key_scope"),
    )
    op.create_index(op.f("ix_idempotency_key_org_id"), "idempotency_key", ["org_id"])
    op.create_index(op.f("ix_idempotency_key_endpoint"), "idempotency_key", ["endpoint"])
    op.create_index(op.f("ix_idempotency_key_key"), "idempotency_key", ["key"])


def downgrade() -> None:
    op.drop_index(op.f("ix_idempotency_key_key"), table_name="idempotency_key")
    op.drop_index(op.f("ix_idempotency_key_endpoint"), table_name="idempotency_key")
    op.drop_index(op.f("ix_idempotency_key_org_id"), table_name="idempotency_key")
    op.drop_table("idempotency_key")

    op.drop_index(op.f("ix_consumed_event_event_id"), table_name="consumed_event")
    op.drop_index(op.f("ix_consumed_event_consumer"), table_name="consumed_event")
    op.drop_table("consumed_event")

    op.drop_index("ix_outbox_event_type_id", table_name="outbox_event")
    op.drop_index("ix_outbox_event_state_id", table_name="outbox_event")
    op.drop_index(op.f("ix_outbox_event_tenant_id"), table_name="outbox_event")
    op.drop_index(op.f("ix_outbox_event_state"), table_name="outbox_event")
    op.drop_index(op.f("ix_outbox_event_event_type"), table_name="outbox_event")
    op.drop_index(op.f("ix_outbox_event_event_id"), table_name="outbox_event")
    op.drop_table("outbox_event")
