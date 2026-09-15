"""add api_source and drift_report

Source connectors, continuous sync and drift detection (ESDS LLD §3.3, build
prompt §12–§13).

Additive. Existing projects get one `api_source` row backfilled from the
provenance already on the project row, so their history starts at the source
they were actually imported from rather than at nothing. Watching is **off**
for every backfilled row: turning polling on for existing projects without
being asked would start making outbound requests nobody consented to.

Revision ID: 0035
Revises: 0034
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0035"
down_revision: Union[str, Sequence[str], None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_source",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("connector", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("config_json", sa.String(), nullable=False),
        sa.Column("token_secret_id", sa.Uuid(), nullable=True),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("source_uri", sa.String(), nullable=False),
        sa.Column("source_version", sa.String(), nullable=True),
        sa.Column("commit_sha", sa.String(), nullable=True),
        sa.Column("etag", sa.String(), nullable=True),
        sa.Column("last_modified", sa.String(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("ir_hash", sa.String(), nullable=True),
        sa.Column("ir_json", sa.String(), nullable=True),
        sa.Column("watch_enabled", sa.Boolean(), nullable=False),
        sa.Column("watch_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("apply_policy", sa.String(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_changed_at", sa.DateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["openapi_project.id"]),
        sa.ForeignKeyConstraint(["token_secret_id"], ["secret.id"]),
        sa.ForeignKeyConstraint(["connection_id"], ["provider_connection.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "connector", "source_uri", name="uq_api_source_locator"),
    )
    op.create_index(op.f("ix_api_source_org_id"), "api_source", ["org_id"])
    op.create_index(op.f("ix_api_source_project_id"), "api_source", ["project_id"])
    op.create_index(op.f("ix_api_source_connector"), "api_source", ["connector"])

    op.create_table(
        "drift_report",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("compared_source_id", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("breaking_count", sa.Integer(), nullable=False),
        sa.Column("non_breaking_count", sa.Integer(), nullable=False),
        sa.Column("security_count", sa.Integer(), nullable=False),
        sa.Column("documentation_count", sa.Integer(), nullable=False),
        sa.Column("metadata_count", sa.Integer(), nullable=False),
        sa.Column("changes_json", sa.String(), nullable=False),
        sa.Column("before_ir_hash", sa.String(), nullable=True),
        sa.Column("after_ir_hash", sa.String(), nullable=True),
        sa.Column("after_spec_text", sa.String(), nullable=True),
        sa.Column("auto_applied", sa.Boolean(), nullable=False),
        sa.Column("withheld_reason", sa.String(), nullable=True),
        sa.Column("detected_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["openapi_project.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["api_source.id"]),
        sa.ForeignKeyConstraint(["compared_source_id"], ["api_source.id"]),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_drift_report_org_id"), "drift_report", ["org_id"])
    op.create_index(op.f("ix_drift_report_project_id"), "drift_report", ["project_id"])
    op.create_index(op.f("ix_drift_report_source_id"), "drift_report", ["source_id"])
    op.create_index(op.f("ix_drift_report_status"), "drift_report", ["status"])

    # Identity of the IR each project holds, so drift has a "before" to compare
    # against without re-deriving it.
    op.add_column("openapi_project", sa.Column("ir_version", sa.Integer(), nullable=True))
    op.add_column("openapi_project", sa.Column("ir_hash", sa.String(), nullable=True))
    op.add_column("openapi_project", sa.Column("content_hash", sa.String(), nullable=True))

    # Backfill one primary source per existing project from what the project
    # already records. Watching stays off: starting outbound polling for
    # projects whose owners never asked for it would be a surprise.
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    new_id = (
        "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || "
        "substr(lower(hex(randomblob(2))), 2) || '-a' || "
        "substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6)))"
        if is_sqlite
        else "gen_random_uuid()"
    )
    # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
    op.execute(
        f"""
        INSERT INTO api_source (
            id, org_id, project_id, connector, role, label, config_json,
            token_secret_id, connection_id, source_uri, source_version, commit_sha,
            etag, last_modified, retrieved_at, content_hash, ir_hash, ir_json,
            watch_enabled, watch_interval_seconds, apply_policy, last_checked_at,
            last_changed_at, consecutive_failures, last_error, created_at, updated_at
        )
        SELECT
            {new_id}, p.org_id, p.id, p.source_kind, 'primary', '', '{{}}',
            NULL, NULL, COALESCE(p.source_url, ''), NULL, NULL,
            NULL, NULL, p.created_at, NULL, NULL, p.ir_json,
            {"0" if is_sqlite else "false"}, 3600, 'never', NULL,
            NULL, 0, NULL, p.created_at, p.updated_at
        FROM openapi_project p
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("openapi_project") as batch:
        batch.drop_column("content_hash")
        batch.drop_column("ir_hash")
        batch.drop_column("ir_version")

    op.drop_index(op.f("ix_drift_report_status"), table_name="drift_report")
    op.drop_index(op.f("ix_drift_report_source_id"), table_name="drift_report")
    op.drop_index(op.f("ix_drift_report_project_id"), table_name="drift_report")
    op.drop_index(op.f("ix_drift_report_org_id"), table_name="drift_report")
    op.drop_table("drift_report")

    op.drop_index(op.f("ix_api_source_connector"), table_name="api_source")
    op.drop_index(op.f("ix_api_source_project_id"), table_name="api_source")
    op.drop_index(op.f("ix_api_source_org_id"), table_name="api_source")
    op.drop_table("api_source")
