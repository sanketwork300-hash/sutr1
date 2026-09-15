"""add pricing plans, the ledger, invoices, settlements and dunning

ESDS LLD §5.1: every financial fact derives from immutable usage records,
prices are evaluated at billing time, and no financial data is ever deleted.

Five new tables and six new columns on `usage_event`. The columns are the
dimensions §5.1.2 names and Sutr did not capture — invocation id, region,
payload size, tokens, the earning provider, and the tool — plus a pricing
*context* that deliberately contains no price.

`uq_usage_event_invocation` is the dedupe §5.1 asks for: a replayed event with
the same invocation id collides instead of double-charging. NULLs do not
collide, so an event whose caller had no id to give is still recorded.

Nothing in this migration deletes anything, and nothing in the code that reads
it can: a ledger entry has no update path and an invoice is voided rather than
removed.

Revision ID: 0041
Revises: 0040
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0041"
down_revision: Union[str, Sequence[str], None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("usage_event") as batch:
        batch.add_column(sa.Column("invocation_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("region", sa.String(), nullable=True))
        batch.add_column(sa.Column("payload_bytes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("provider_org_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("tool_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("pricing_context_json", sa.String(), nullable=False, server_default="{}")
        )
        batch.create_foreign_key("fk_usage_event_provider_org", "org", ["provider_org_id"], ["id"])
        batch.create_foreign_key("fk_usage_event_tool", "registry_tool", ["tool_id"], ["id"])
    op.create_index("ix_usage_event_invocation_id", "usage_event", ["invocation_id"])
    op.create_index("ix_usage_event_provider_org_id", "usage_event", ["provider_org_id"])
    op.create_index("ix_usage_event_tool_id", "usage_event", ["tool_id"])
    op.create_index(
        "uq_usage_event_invocation",
        "usage_event",
        ["org_id", "invocation_id"],
        unique=True,
    )

    op.create_table(
        "pricing_plan",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("amount_micros", sa.Integer(), nullable=False),
        sa.Column("included_units", sa.Integer(), nullable=False),
        sa.Column("base_micros", sa.Integer(), nullable=False),
        sa.Column("tiers_json", sa.String(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("usage_kind", sa.String(), nullable=False),
        sa.Column("discount_bps", sa.Integer(), nullable=False),
        sa.Column("tax_bps", sa.Integer(), nullable=False),
        sa.Column("tax_label", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["tool_id"], ["registry_tool.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "key", "version", name="uq_pricing_plan_version"),
    )
    op.create_index("ix_pricing_plan_org_id", "pricing_plan", ["org_id"])
    op.create_index("ix_pricing_plan_key", "pricing_plan", ["key"])
    op.create_index("ix_pricing_plan_state", "pricing_plan", ["state"])
    op.create_index("ix_pricing_plan_model", "pricing_plan", ["model"])
    op.create_index("ix_pricing_plan_tool_id", "pricing_plan", ["tool_id"])
    op.create_index("ix_pricing_plan_usage_kind", "pricing_plan", ["usage_kind"])

    op.create_table(
        "invoice",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("subtotal_micros", sa.Integer(), nullable=False),
        sa.Column("discount_micros", sa.Integer(), nullable=False),
        sa.Column("tax_micros", sa.Integer(), nullable=False),
        sa.Column("total_micros", sa.Integer(), nullable=False),
        sa.Column("plan_versions_json", sa.String(), nullable=False),
        sa.Column("failure_reason", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("void_reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "period_start", "period_end", name="uq_invoice_period"),
    )
    op.create_index("ix_invoice_org_id", "invoice", ["org_id"])
    op.create_index("ix_invoice_number", "invoice", ["number"])
    op.create_index("ix_invoice_state", "invoice", ["state"])
    op.create_index("ix_invoice_period_start", "invoice", ["period_start"])
    op.create_index("ix_invoice_period_end", "invoice", ["period_end"])

    op.create_table(
        "invoice_line",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("usage_kind", sa.String(), nullable=False),
        sa.Column("integration_id", sa.String(), nullable=True),
        sa.Column("tool_id", sa.Uuid(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=True),
        sa.Column("plan_key", sa.String(), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("plan_model", sa.String(), nullable=False),
        sa.Column("subtotal_micros", sa.Integer(), nullable=False),
        sa.Column("discount_micros", sa.Integer(), nullable=False),
        sa.Column("tax_micros", sa.Integer(), nullable=False),
        sa.Column("total_micros", sa.Integer(), nullable=False),
        sa.Column("breakdown_json", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["tool_id"], ["registry_tool.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["pricing_plan.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_invoice_line_invoice_id", "invoice_line", ["invoice_id"])
    op.create_index("ix_invoice_line_org_id", "invoice_line", ["org_id"])

    op.create_table(
        "settlement",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_org_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("gross_micros", sa.Integer(), nullable=False),
        sa.Column("provider_micros", sa.Integer(), nullable=False),
        sa.Column("platform_micros", sa.Integer(), nullable=False),
        sa.Column("provider_share_bps", sa.Integer(), nullable=False),
        sa.Column("invoice_ids_json", sa.String(), nullable=False),
        sa.Column("failure_reason", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("paid_out", sa.Boolean(), nullable=False),
        sa.Column("payout_reference", sa.String(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["provider_org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_settlement_provider_org_id", "settlement", ["provider_org_id"])
    op.create_index("ix_settlement_state", "settlement", ["state"])
    op.create_index("ix_settlement_period_start", "settlement", ["period_start"])
    op.create_index("ix_settlement_period_end", "settlement", ["period_end"])

    op.create_table(
        "ledger_entry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("account", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("amount_micros", sa.Integer(), nullable=False),
        sa.Column("balance_micros", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("usage_event_id", sa.Integer(), nullable=True),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("settlement_id", sa.Uuid(), nullable=True),
        sa.Column("reverses_entry_id", sa.Uuid(), nullable=True),
        sa.Column("metadata_json", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["usage_event_id"], ["usage_event.id"]),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice.id"]),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlement.id"]),
        sa.ForeignKeyConstraint(["reverses_entry_id"], ["ledger_entry.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ledger_entry_org_id", "ledger_entry", ["org_id"])
    op.create_index("ix_ledger_entry_account", "ledger_entry", ["account"])
    op.create_index("ix_ledger_entry_kind", "ledger_entry", ["kind"])
    op.create_index("ix_ledger_entry_direction", "ledger_entry", ["direction"])
    op.create_index("ix_ledger_entry_invoice_id", "ledger_entry", ["invoice_id"])
    op.create_index("ix_ledger_entry_settlement_id", "ledger_entry", ["settlement_id"])
    op.create_index("ix_ledger_entry_created_at", "ledger_entry", ["created_at"])

    op.create_table(
        "payment_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=False),
        sa.Column("failure_message", sa.String(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("processor", sa.String(), nullable=False),
        sa.Column("reference", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payment_attempt_invoice_id", "payment_attempt", ["invoice_id"])
    op.create_index("ix_payment_attempt_org_id", "payment_attempt", ["org_id"])
    op.create_index("ix_payment_attempt_next_attempt_at", "payment_attempt", ["next_attempt_at"])


def downgrade() -> None:
    for index, table in (
        ("ix_payment_attempt_next_attempt_at", "payment_attempt"),
        ("ix_payment_attempt_org_id", "payment_attempt"),
        ("ix_payment_attempt_invoice_id", "payment_attempt"),
        ("ix_ledger_entry_created_at", "ledger_entry"),
        ("ix_ledger_entry_settlement_id", "ledger_entry"),
        ("ix_ledger_entry_invoice_id", "ledger_entry"),
        ("ix_ledger_entry_direction", "ledger_entry"),
        ("ix_ledger_entry_kind", "ledger_entry"),
        ("ix_ledger_entry_account", "ledger_entry"),
        ("ix_ledger_entry_org_id", "ledger_entry"),
        ("ix_settlement_period_end", "settlement"),
        ("ix_settlement_period_start", "settlement"),
        ("ix_settlement_state", "settlement"),
        ("ix_settlement_provider_org_id", "settlement"),
        ("ix_invoice_line_org_id", "invoice_line"),
        ("ix_invoice_line_invoice_id", "invoice_line"),
        ("ix_invoice_period_end", "invoice"),
        ("ix_invoice_period_start", "invoice"),
        ("ix_invoice_state", "invoice"),
        ("ix_invoice_number", "invoice"),
        ("ix_invoice_org_id", "invoice"),
        ("ix_pricing_plan_usage_kind", "pricing_plan"),
        ("ix_pricing_plan_tool_id", "pricing_plan"),
        ("ix_pricing_plan_model", "pricing_plan"),
        ("ix_pricing_plan_state", "pricing_plan"),
        ("ix_pricing_plan_key", "pricing_plan"),
        ("ix_pricing_plan_org_id", "pricing_plan"),
        ("uq_usage_event_invocation", "usage_event"),
        ("ix_usage_event_tool_id", "usage_event"),
        ("ix_usage_event_provider_org_id", "usage_event"),
        ("ix_usage_event_invocation_id", "usage_event"),
    ):
        op.drop_index(index, table_name=table)

    op.drop_table("payment_attempt")
    op.drop_table("ledger_entry")
    op.drop_table("settlement")
    op.drop_table("invoice_line")
    op.drop_table("invoice")
    op.drop_table("pricing_plan")

    with op.batch_alter_table("usage_event") as batch:
        batch.drop_constraint("fk_usage_event_tool", type_="foreignkey")
        batch.drop_constraint("fk_usage_event_provider_org", type_="foreignkey")
        batch.drop_column("pricing_context_json")
        batch.drop_column("tool_id")
        batch.drop_column("provider_org_id")
        batch.drop_column("tokens")
        batch.drop_column("payload_bytes")
        batch.drop_column("region")
        batch.drop_column("invocation_id")
