"""add the registry and the marketplace projection

The Registry as authoritative system of record and the Marketplace as a
storefront derived from its events (ESDS LLD §3.7): tools, immutable versions,
governance-gated change requests, pricing history, provider profiles,
subscriptions, and the projected listings.

Purely additive: seven new tables and no change to any existing one. The
existing `/api/marketplace` catalog over bundled and compiled integrations is
untouched and keeps working; a registry-backed listing is what fills in the
fields it has been returning as explicit nulls.

Two unique constraints carry design decisions rather than hygiene:
`uq_registry_tool_key` makes a tool's identity stable within a tenant, and
`uq_registry_version_number` is what makes versions immutable and monotonic —
a rewritten v2 would have to collide with the v2 that already exists.

Revision ID: 0038
Revises: 0037
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0038"
down_revision: Union[str, Sequence[str], None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "registry_tool",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("tool_key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("tags_json", sa.String(), nullable=False),
        sa.Column("regions_json", sa.String(), nullable=False),
        sa.Column("compliance_json", sa.String(), nullable=False),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.Column("lifecycle_state", sa.String(), nullable=False),
        sa.Column("state_reason", sa.String(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("integration_id", sa.String(), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("published_version", sa.Integer(), nullable=True),
        sa.Column("deprecation_note", sa.String(), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["openapi_project.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "tool_key", name="uq_registry_tool_key"),
    )
    op.create_index("ix_registry_tool_org_id", "registry_tool", ["org_id"])
    op.create_index("ix_registry_tool_tool_key", "registry_tool", ["tool_key"])
    op.create_index("ix_registry_tool_visibility", "registry_tool", ["visibility"])
    op.create_index("ix_registry_tool_lifecycle_state", "registry_tool", ["lifecycle_state"])
    op.create_index("ix_registry_tool_integration_id", "registry_tool", ["integration_id"])

    op.create_table(
        "registry_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        sa.Column("build_hash", sa.String(), nullable=False),
        sa.Column("sbom_json", sa.String(), nullable=False),
        sa.Column("deployment_manifest_json", sa.String(), nullable=False),
        sa.Column("tool_count", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(), nullable=False),
        sa.Column("was_published", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tool_id"], ["registry_tool.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["artifact_id"], ["runtime_artifact.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tool_id", "version", name="uq_registry_version_number"),
    )
    op.create_index("ix_registry_version_tool_id", "registry_version", ["tool_id"])
    op.create_index("ix_registry_version_org_id", "registry_version", ["org_id"])

    op.create_table(
        "registry_change_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("current_json", sa.String(), nullable=False),
        sa.Column("requested_json", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decision_note", sa.String(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["tool_id"], ["registry_tool.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_registry_change_request_org_id", "registry_change_request", ["org_id"])
    op.create_index("ix_registry_change_request_tool_id", "registry_change_request", ["tool_id"])
    op.create_index("ix_registry_change_request_kind", "registry_change_request", ["kind"])
    op.create_index("ix_registry_change_request_status", "registry_change_request", ["status"])

    op.create_table(
        "registry_pricing",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("amount_micros", sa.Integer(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("free_allowance", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tool_id"], ["registry_tool.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_registry_pricing_tool_id", "registry_pricing", ["tool_id"])
    op.create_index("ix_registry_pricing_org_id", "registry_pricing", ["org_id"])
    op.create_index("ix_registry_pricing_superseded_at", "registry_pricing", ["superseded_at"])

    op.create_table(
        "provider_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("website_url", sa.String(), nullable=False),
        sa.Column("support_email", sa.String(), nullable=False),
        sa.Column("support_url", sa.String(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("verified_note", sa.String(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_profile_org_id", "provider_profile", ["org_id"], unique=True)

    op.create_table(
        "tool_subscription",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("provider_org_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("pricing_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("subscribed_at", sa.DateTime(), nullable=False),
        sa.Column("provisioned_at", sa.DateTime(), nullable=True),
        sa.Column("renews_at", sa.DateTime(), nullable=True),
        sa.Column("renewed_count", sa.Integer(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_reason", sa.String(), nullable=False),
        sa.Column("failure_reason", sa.String(), nullable=False),
        sa.Column("subscribed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["tool_id"], ["registry_tool.id"]),
        sa.ForeignKeyConstraint(["provider_org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["pricing_id"], ["registry_pricing.id"]),
        sa.ForeignKeyConstraint(["subscribed_by_user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_subscription_org_id", "tool_subscription", ["org_id"])
    op.create_index("ix_tool_subscription_tool_id", "tool_subscription", ["tool_id"])
    op.create_index(
        "ix_tool_subscription_provider_org_id", "tool_subscription", ["provider_org_id"]
    )
    op.create_index("ix_tool_subscription_state", "tool_subscription", ["state"])

    op.create_table(
        "marketplace_listing",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("provider_org_id", sa.Uuid(), nullable=False),
        sa.Column("tool_key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("tags_json", sa.String(), nullable=False),
        sa.Column("regions_json", sa.String(), nullable=False),
        sa.Column("compliance_json", sa.String(), nullable=False),
        sa.Column("integration_id", sa.String(), nullable=True),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.Column("lifecycle_state", sa.String(), nullable=False),
        sa.Column("deprecation_note", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("versions_json", sa.String(), nullable=False),
        sa.Column("pricing_json", sa.String(), nullable=False),
        sa.Column("provider_json", sa.String(), nullable=False),
        sa.Column("trust_score", sa.Integer(), nullable=True),
        sa.Column("trust_json", sa.String(), nullable=False),
        sa.Column("subscriber_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("source_event_id", sa.String(), nullable=False),
        sa.Column("source_event_type", sa.String(), nullable=False),
        sa.Column("projected_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tool_id"], ["registry_tool.id"]),
        sa.ForeignKeyConstraint(["provider_org_id"], ["org.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_marketplace_listing_tool_id", "marketplace_listing", ["tool_id"], unique=True
    )
    op.create_index(
        "ix_marketplace_listing_provider_org_id", "marketplace_listing", ["provider_org_id"]
    )
    op.create_index("ix_marketplace_listing_category", "marketplace_listing", ["category"])
    op.create_index(
        "ix_marketplace_listing_integration_id", "marketplace_listing", ["integration_id"]
    )
    op.create_index("ix_marketplace_listing_trust_score", "marketplace_listing", ["trust_score"])
    op.create_index("ix_marketplace_listing_state", "marketplace_listing", ["state"])
    op.create_index(
        "ix_marketplace_listing_lifecycle_state", "marketplace_listing", ["lifecycle_state"]
    )
    op.create_index("ix_marketplace_listing_visibility", "marketplace_listing", ["visibility"])


def downgrade() -> None:
    for index, table in (
        ("ix_marketplace_listing_visibility", "marketplace_listing"),
        ("ix_marketplace_listing_lifecycle_state", "marketplace_listing"),
        ("ix_marketplace_listing_state", "marketplace_listing"),
        ("ix_marketplace_listing_trust_score", "marketplace_listing"),
        ("ix_marketplace_listing_integration_id", "marketplace_listing"),
        ("ix_marketplace_listing_category", "marketplace_listing"),
        ("ix_marketplace_listing_provider_org_id", "marketplace_listing"),
        ("ix_marketplace_listing_tool_id", "marketplace_listing"),
        ("ix_tool_subscription_state", "tool_subscription"),
        ("ix_tool_subscription_provider_org_id", "tool_subscription"),
        ("ix_tool_subscription_tool_id", "tool_subscription"),
        ("ix_tool_subscription_org_id", "tool_subscription"),
        ("ix_provider_profile_org_id", "provider_profile"),
        ("ix_registry_pricing_superseded_at", "registry_pricing"),
        ("ix_registry_pricing_org_id", "registry_pricing"),
        ("ix_registry_pricing_tool_id", "registry_pricing"),
        ("ix_registry_change_request_status", "registry_change_request"),
        ("ix_registry_change_request_kind", "registry_change_request"),
        ("ix_registry_change_request_tool_id", "registry_change_request"),
        ("ix_registry_change_request_org_id", "registry_change_request"),
        ("ix_registry_version_org_id", "registry_version"),
        ("ix_registry_version_tool_id", "registry_version"),
        ("ix_registry_tool_integration_id", "registry_tool"),
        ("ix_registry_tool_lifecycle_state", "registry_tool"),
        ("ix_registry_tool_visibility", "registry_tool"),
        ("ix_registry_tool_tool_key", "registry_tool"),
        ("ix_registry_tool_org_id", "registry_tool"),
    ):
        op.drop_index(index, table_name=table)

    # Reverse dependency order: listings and subscriptions point at tools and
    # pricing, versions point at tools, tools point at nothing new.
    op.drop_table("marketplace_listing")
    op.drop_table("tool_subscription")
    op.drop_table("provider_profile")
    op.drop_table("registry_pricing")
    op.drop_table("registry_change_request")
    op.drop_table("registry_version")
    op.drop_table("registry_tool")
