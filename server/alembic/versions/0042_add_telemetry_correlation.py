"""join a tenant's own call log to the platform trace

ESDS LLD §5.3 asks for tenant-isolated telemetry and for traces that span
Gateway → Discovery → Runtime → Provider. The traces themselves live in a trace
backend, which a tenant has no access to and should not: it holds every
tenant's spans. What a tenant can be given is the operational record this
platform already keeps for it — and the ids that let an operator, holding both,
line the two up.

So `log_entry` gains the correlation id of the operation the call belonged to
and, when tracing was running, the trace id of the trace that recorded it. Both
are nullable: rows written before this migration have neither, and a call made
with tracing off has no trace id to record. A guessed id would be worse than a
null one — it would send a reader to a trace that does not exist.

Revision ID: 0042
Revises: 0041
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0042"
down_revision: Union[str, Sequence[str], None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("log_entry") as batch:
        batch.add_column(sa.Column("correlation_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("trace_id", sa.String(), nullable=True))
    # Always read together with org_id: a tenant asking for one operation's
    # timeline is asking within its own rows, and the index has to say so or
    # the query would scan another tenant's log to answer it.
    op.create_index(
        "ix_log_entry_org_correlation",
        "log_entry",
        ["org_id", "correlation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_log_entry_org_correlation", table_name="log_entry")
    with op.batch_alter_table("log_entry") as batch:
        batch.drop_column("trace_id")
        batch.drop_column("correlation_id")
