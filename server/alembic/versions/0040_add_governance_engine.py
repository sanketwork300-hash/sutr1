"""add the governance, compliance and risk engine

ESDS LLD §5.2: versioned policies with a seven-state lifecycle, compliance runs
against named frameworks, explainable risk assessments, the six-stage approval
workflow, and time-boxed exceptions.

Purely additive: six new tables, no change to any existing one. A tenant that
writes no policies is unaffected — an access policy with no active version
contributes no rules, and the decision point keeps the behaviour it had.

Two constraints carry design decisions. `uq_governance_policy_version` makes a
version immutable in the only way a schema can: a rewritten v2 would have to
collide with the v2 that exists. And `governance_exception.expires_at` is NOT
NULL because the LLD says *"all exceptions expire"* — a nullable column would
make a permanent exception representable, and anything representable eventually
gets written.

Revision ID: 0040
Revises: 0039
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0040"
down_revision: Union[str, Sequence[str], None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "governance_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=True),
        sa.Column("previous_active_version", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "key", name="uq_governance_policy_key"),
    )
    op.create_index("ix_governance_policy_org_id", "governance_policy", ["org_id"])
    op.create_index("ix_governance_policy_key", "governance_policy", ["key"])
    op.create_index("ix_governance_policy_kind", "governance_policy", ["kind"])
    op.create_index("ix_governance_policy_active_version", "governance_policy", ["active_version"])

    op.create_table(
        "governance_policy_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("document_json", sa.String(), nullable=False),
        sa.Column("notes", sa.String(), nullable=False),
        sa.Column("decision_note", sa.String(), nullable=False),
        sa.Column("authored_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("deprecated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["policy_id"], ["governance_policy.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["authored_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_id", "version", name="uq_governance_policy_version"),
    )
    op.create_index(
        "ix_governance_policy_version_policy_id", "governance_policy_version", ["policy_id"]
    )
    op.create_index("ix_governance_policy_version_org_id", "governance_policy_version", ["org_id"])
    op.create_index("ix_governance_policy_version_state", "governance_policy_version", ["state"])

    op.create_table(
        "compliance_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("framework", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("results_json", sa.String(), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("manual", sa.Integer(), nullable=False),
        sa.Column("not_applicable", sa.Integer(), nullable=False),
        sa.Column("failure_reason", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_compliance_run_org_id", "compliance_run", ["org_id"])
    op.create_index("ix_compliance_run_framework", "compliance_run", ["framework"])
    op.create_index("ix_compliance_run_target_type", "compliance_run", ["target_type"])
    op.create_index("ix_compliance_run_target_id", "compliance_run", ["target_id"])
    op.create_index("ix_compliance_run_state", "compliance_run", ["state"])

    op.create_table(
        "risk_assessment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("components_json", sa.String(), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False),
        sa.Column("unavailable_reason", sa.String(), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("stale_reason", sa.String(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_risk_assessment_org_id", "risk_assessment", ["org_id"])
    op.create_index("ix_risk_assessment_target_type", "risk_assessment", ["target_type"])
    op.create_index("ix_risk_assessment_target_id", "risk_assessment", ["target_id"])
    op.create_index("ix_risk_assessment_stale", "risk_assessment", ["stale"])

    op.create_table(
        "governance_review",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("stages_json", sa.String(), nullable=False),
        sa.Column("current_stage", sa.String(), nullable=False),
        sa.Column("blocked_reason", sa.String(), nullable=False),
        sa.Column("risk_score", sa.Integer(), nullable=True),
        sa.Column("auto_approved", sa.Boolean(), nullable=False),
        sa.Column("decision_note", sa.String(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["tool_id"], ["registry_tool.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tool_id", "opened_at", name="uq_governance_review_open"),
    )
    op.create_index("ix_governance_review_org_id", "governance_review", ["org_id"])
    op.create_index("ix_governance_review_tool_id", "governance_review", ["tool_id"])
    op.create_index("ix_governance_review_state", "governance_review", ["state"])
    op.create_index("ix_governance_review_current_stage", "governance_review", ["current_stage"])

    op.create_table(
        "governance_exception",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("control", sa.String(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("violation", sa.String(), nullable=False),
        sa.Column("justification", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("risk_json", sa.String(), nullable=False),
        sa.Column("decision_note", sa.String(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        # Not nullable: all exceptions expire (LLD §5.2.9).
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revalidate_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["governance_policy.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_governance_exception_org_id", "governance_exception", ["org_id"])
    op.create_index("ix_governance_exception_control", "governance_exception", ["control"])
    op.create_index("ix_governance_exception_scope_type", "governance_exception", ["scope_type"])
    op.create_index("ix_governance_exception_scope_id", "governance_exception", ["scope_id"])
    op.create_index("ix_governance_exception_state", "governance_exception", ["state"])
    op.create_index("ix_governance_exception_expires_at", "governance_exception", ["expires_at"])


def downgrade() -> None:
    for index, table in (
        ("ix_governance_exception_expires_at", "governance_exception"),
        ("ix_governance_exception_state", "governance_exception"),
        ("ix_governance_exception_scope_id", "governance_exception"),
        ("ix_governance_exception_scope_type", "governance_exception"),
        ("ix_governance_exception_control", "governance_exception"),
        ("ix_governance_exception_org_id", "governance_exception"),
        ("ix_governance_review_current_stage", "governance_review"),
        ("ix_governance_review_state", "governance_review"),
        ("ix_governance_review_tool_id", "governance_review"),
        ("ix_governance_review_org_id", "governance_review"),
        ("ix_risk_assessment_stale", "risk_assessment"),
        ("ix_risk_assessment_target_id", "risk_assessment"),
        ("ix_risk_assessment_target_type", "risk_assessment"),
        ("ix_risk_assessment_org_id", "risk_assessment"),
        ("ix_compliance_run_state", "compliance_run"),
        ("ix_compliance_run_target_id", "compliance_run"),
        ("ix_compliance_run_target_type", "compliance_run"),
        ("ix_compliance_run_framework", "compliance_run"),
        ("ix_compliance_run_org_id", "compliance_run"),
        ("ix_governance_policy_version_state", "governance_policy_version"),
        ("ix_governance_policy_version_org_id", "governance_policy_version"),
        ("ix_governance_policy_version_policy_id", "governance_policy_version"),
        ("ix_governance_policy_active_version", "governance_policy"),
        ("ix_governance_policy_kind", "governance_policy"),
        ("ix_governance_policy_key", "governance_policy"),
        ("ix_governance_policy_org_id", "governance_policy"),
    ):
        op.drop_index(index, table_name=table)

    op.drop_table("governance_exception")
    op.drop_table("governance_review")
    op.drop_table("risk_assessment")
    op.drop_table("compliance_run")
    op.drop_table("governance_policy_version")
    op.drop_table("governance_policy")
