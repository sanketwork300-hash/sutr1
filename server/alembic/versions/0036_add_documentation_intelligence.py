"""add documentation intelligence tables

The Documentation Intelligence service (ESDS LLD §3.5, build prompt §15–§16):
documents, per-stage processing jobs, chunks, extracted business rules,
workflows and glossary terms, a knowledge graph, and chunk embeddings.

Purely additive — seven new tables and no change to any existing one. Nothing
in the platform reads them yet apart from the documentation API, so an install
that never uploads a document is unaffected by this migration beyond the
schema itself.

`document.content` is a LargeBinary because the default storage backend keeps
originals in the database (ADR-015); the filesystem backend leaves it empty.
Every extracted fact has to be able to cite the sentence it came from, so the
original is evidence, not a cache.

Revision ID: 0036
Revises: 0035
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0036"
down_revision: Union[str, Sequence[str], None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_uri", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("document_type", sa.String(), nullable=False),
        sa.Column("classification_json", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("degradations_json", sa.String(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["openapi_project.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_org_id", "document", ["org_id"])
    op.create_index("ix_document_project_id", "document", ["project_id"])
    op.create_index("ix_document_sha256", "document", ["sha256"])
    op.create_index("ix_document_status", "document", ["status"])
    op.create_index("ix_document_document_type", "document", ["document_type"])

    op.create_table(
        "document_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_stage", sa.String(), nullable=True),
        sa.Column("completed_stages_json", sa.String(), nullable=False),
        sa.Column("stage_results_json", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("degradations_json", sa.String(), nullable=False),
        sa.Column("extractor_version", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_job_org_id", "document_job", ["org_id"])
    op.create_index("ix_document_job_document_id", "document_job", ["document_id"])
    op.create_index("ix_document_job_status", "document_job", ["status"])

    op.create_table(
        "document_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.String(), nullable=False),
        sa.Column("element_type", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_chunk_org_id", "document_chunk", ["org_id"])
    op.create_index("ix_document_chunk_document_id", "document_chunk", ["document_id"])
    op.create_index("ix_document_chunk_doc_order", "document_chunk", ["document_id", "position"])

    op.create_table(
        "business_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("rule_type", sa.String(), nullable=False),
        sa.Column("condition", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("source_document", sa.String(), nullable=False),
        sa.Column("source_location", sa.String(), nullable=False),
        sa.Column("source_text", sa.String(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("extractor_version", sa.String(), nullable=False),
        sa.Column("signals_json", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunk.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["document_job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_business_rule_org_id", "business_rule", ["org_id"])
    op.create_index("ix_business_rule_document_id", "business_rule", ["document_id"])
    op.create_index("ix_business_rule_rule_type", "business_rule", ["rule_type"])

    op.create_table(
        "doc_workflow",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("nodes_json", sa.String(), nullable=False),
        sa.Column("edges_json", sa.String(), nullable=False),
        sa.Column("operation_refs_json", sa.String(), nullable=False),
        sa.Column("tool_refs_json", sa.String(), nullable=False),
        sa.Column("source_document", sa.String(), nullable=False),
        sa.Column("source_location", sa.String(), nullable=False),
        sa.Column("source_text", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("extractor_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunk.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["document_job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_doc_workflow_org_id", "doc_workflow", ["org_id"])
    op.create_index("ix_doc_workflow_document_id", "doc_workflow", ["document_id"])

    op.create_table(
        "glossary_term",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("term", sa.String(), nullable=False),
        sa.Column("definition", sa.String(), nullable=False),
        sa.Column("aliases_json", sa.String(), nullable=False),
        sa.Column("source_document", sa.String(), nullable=False),
        sa.Column("source_location", sa.String(), nullable=False),
        sa.Column("source_text", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("extractor_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunk.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["document_job.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_glossary_term_org_id", "glossary_term", ["org_id"])
    op.create_index("ix_glossary_term_document_id", "glossary_term", ["document_id"])
    op.create_index("ix_glossary_term_term", "glossary_term", ["term"])

    op.create_table(
        "knowledge_node",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_key", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("properties_json", sa.String(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Re-processing a document must update the graph, not duplicate it.
        sa.UniqueConstraint(
            "org_id", "entity_type", "entity_key", name="uq_knowledge_node_identity"
        ),
    )
    op.create_index("ix_knowledge_node_org_id", "knowledge_node", ["org_id"])
    op.create_index("ix_knowledge_node_entity_type", "knowledge_node", ["entity_type"])
    op.create_index("ix_knowledge_node_entity_key", "knowledge_node", ["entity_key"])

    op.create_table(
        "knowledge_edge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("source_node_id", sa.Uuid(), nullable=False),
        sa.Column("target_node_id", sa.Uuid(), nullable=False),
        sa.Column("relationship", sa.String(), nullable=False),
        sa.Column("properties_json", sa.String(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["source_node_id"], ["knowledge_node.id"]),
        sa.ForeignKeyConstraint(["target_node_id"], ["knowledge_node.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "source_node_id",
            "relationship",
            "target_node_id",
            name="uq_knowledge_edge_identity",
        ),
    )
    op.create_index("ix_knowledge_edge_org_id", "knowledge_edge", ["org_id"])
    op.create_index("ix_knowledge_edge_relationship", "knowledge_edge", ["relationship"])
    op.create_index("ix_knowledge_edge_source", "knowledge_edge", ["org_id", "source_node_id"])
    op.create_index("ix_knowledge_edge_target", "knowledge_edge", ["org_id", "target_node_id"])

    op.create_table(
        "chunk_embedding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("norm", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["org.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["document.id"]),
        sa.ForeignKeyConstraint(["chunk_id"], ["document_chunk.id"]),
        sa.PrimaryKeyConstraint("id"),
        # Two models' vectors under one chunk would be silently averaged by a
        # naive query; keying by (chunk, model) makes that impossible.
        sa.UniqueConstraint("chunk_id", "model", name="uq_chunk_embedding_model"),
    )
    op.create_index("ix_chunk_embedding_org_id", "chunk_embedding", ["org_id"])
    op.create_index("ix_chunk_embedding_document_id", "chunk_embedding", ["document_id"])
    op.create_index("ix_chunk_embedding_chunk_id", "chunk_embedding", ["chunk_id"])
    op.create_index("ix_chunk_embedding_model", "chunk_embedding", ["model"])


def downgrade() -> None:
    # Reverse creation order: edges before nodes, facts before chunks,
    # everything before the document they hang off.
    for table in (
        "chunk_embedding",
        "knowledge_edge",
        "knowledge_node",
        "glossary_term",
        "doc_workflow",
        "business_rule",
        "document_chunk",
        "document_job",
        "document",
    ):
        op.drop_table(table)
