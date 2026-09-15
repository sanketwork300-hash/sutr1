"""Documentation: upload a document, process it, and query what it said.

The LLD's interface is `POST /v1/documentation/jobs {provider_id,
document_uri, document_type} → job_id`, and that is exactly what
`create_job` accepts. It also takes a multipart upload, because the common
case is a person with a PDF rather than a service with a URL, and a URI-only
interface would just mean everyone builds a file server first.

Processing runs inline. The LLD puts documentation processing on a queue, and
that is the right shape at scale — but there is no worker process in this
install, and a job row that says "queued" forever with nothing to run it would
be a worse lie than a slow request (ADR-030). The stage checkpoints in
`documentation/jobs.py` are what makes moving to a worker a change of caller
rather than a rewrite.

Everything here is scoped to the caller's org, in the query, on every route.
"""

import json
import uuid

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, col, delete, select

from sutr.authz import ensure_agent_can
from sutr.common import envelope
from sutr.common.errors import InvalidRequestError, NotFoundError
from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_agent_auth
from sutr.documentation import embeddings, ingestion, jobs, retrieval, storage
from sutr.documentation.classification import DOCUMENT_TYPES
from sutr.documentation.extraction import describe_extractors
from sutr.documentation.graph import neighbours
from sutr.documentation.parsing import parser_capabilities
from sutr.models.business_rule import BusinessRule
from sutr.models.chunk_embedding import ChunkEmbedding
from sutr.models.doc_workflow import DocWorkflow, GlossaryTerm
from sutr.models.document import Document
from sutr.models.document_chunk import DocumentChunk
from sutr.models.document_job import DocumentJob
from sutr.models.knowledge_graph import KnowledgeEdge, KnowledgeNode
from sutr.services.audit import actor_from_agent_auth, record_audit

router = APIRouter(prefix="/v1/documentation", tags=["documentation"])

# Reading extracted knowledge is a read; uploading and reprocessing is a
# change to what the platform believes about a provider, so it is not.
READ = "logs:read"
WRITE = "integrations:manage"


class CreateJobRequest(BaseModel):
    """The LLD §3.5 job interface.

    `provider_id` is Sutr's OpenAPI project: the API this documentation
    describes. Optional, because a policy document or a glossary need not
    belong to any single API.
    """

    provider_id: uuid.UUID | None = None
    document_uri: str | None = None
    document_type: str | None = None
    # Inline content, base64-free: for small text documents and for tests.
    content: str | None = None
    filename: str = ""
    extractor: str | None = None


def _load_document(session: Session, document_id: uuid.UUID, org_id: uuid.UUID) -> Document:
    document = session.get(Document, document_id)
    if document is None or document.org_id != org_id:
        # Same answer for "does not exist" and "belongs to another tenant":
        # distinguishing them turns this endpoint into an existence oracle.
        raise NotFoundError(f"Document {document_id} was not found.")
    return document


def _serialize_document(document: Document) -> dict:
    return {
        "id": str(document.id),
        "project_id": str(document.project_id) if document.project_id else None,
        "filename": document.filename,
        "media_type": document.media_type,
        "kind": document.kind,
        "document_type": document.document_type,
        "classification": json.loads(document.classification_json or "{}"),
        "source_uri": document.source_uri,
        "size_bytes": document.size_bytes,
        "sha256": document.sha256,
        "status": document.status,
        "degradations": json.loads(document.degradations_json or "[]"),
        "uploaded_at": document.uploaded_at.isoformat(),
        "processed_at": document.processed_at.isoformat() if document.processed_at else None,
    }


def _serialize_job(job: DocumentJob) -> dict:
    return {
        "id": str(job.id),
        "document_id": str(job.document_id),
        "status": job.status,
        "current_stage": job.current_stage,
        "completed_stages": json.loads(job.completed_stages_json or "[]"),
        "stages": json.loads(job.stage_results_json or "[]"),
        "degradations": json.loads(job.degradations_json or "[]"),
        "extractor_version": job.extractor_version,
        "error": (
            {"code": job.error_code, "message": job.error_message} if job.error_code else None
        ),
        "attempts": job.attempts,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _citation(row) -> dict:
    """The provenance every extracted fact carries. Never optional."""
    return {
        "document_id": str(row.document_id),
        "document": row.source_document,
        "location": row.source_location,
        "text": row.source_text,
        "chunk_id": str(row.chunk_id) if row.chunk_id else None,
    }


@router.get("/capabilities")
def capabilities(
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """What this deployment can actually do to a document.

    Reported rather than assumed: PDF parsing needs a library or a binary, OCR
    needs Tesseract, and semantic search needs an embedding provider. A caller
    should be able to find out that a scanned PDF will come back empty *before*
    uploading it, and to see that search is lexical rather than discovering it
    from disappointing results.
    """
    ensure_agent_can(session, agent_auth, READ)
    return envelope(
        {
            "parsers": parser_capabilities(),
            "extractors": describe_extractors(),
            "embeddings": embeddings.describe(),
            "storage": storage.describe(),
            "document_types": list(DOCUMENT_TYPES),
        }
    )


@router.post("/jobs", status_code=201)
async def create_job(
    body: CreateJobRequest,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The LLD §3.5 job interface: `{provider_id, document_uri, document_type}`.

    Two ways in: a `document_uri` we fetch (SSRF-screened like every other
    outbound fetch), or inline `content` for small text documents. File uploads
    go to `POST /documents` — FastAPI cannot serve JSON and multipart from one
    route, and forcing them together would make the LLD's own interface the
    awkward one.
    """
    ensure_agent_can(session, agent_auth, WRITE)
    if body.document_uri:
        result = await ingestion.ingest_uri(
            session,
            org_id=agent_auth.org.id,
            uri=body.document_uri,
            project_id=body.provider_id,
            declared_kind=body.document_type,
        )
    elif body.content:
        result = ingestion.ingest_bytes(
            session,
            org_id=agent_auth.org.id,
            data=body.content.encode(),
            filename=body.filename or "inline.md",
            # No media type is declared: the filename and the content are the
            # evidence, and asserting `text/plain` would override both.
            media_type="",
            project_id=body.provider_id,
            declared_kind=body.document_type,
        )
    else:
        raise InvalidRequestError(
            "Provide a document_uri or inline content; upload files to POST /documents."
        )
    return await _process(session, agent_auth, result, body.extractor)


@router.post("/documents", status_code=201)
async def upload_document(
    file: UploadFile = File(),
    provider_id: uuid.UUID | None = Form(default=None),
    document_type: str | None = Form(default=None),
    extractor: str | None = Form(default=None),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Upload a document directly. Same pipeline, same response as `/jobs`."""
    ensure_agent_can(session, agent_auth, WRITE)
    result = ingestion.ingest_bytes(
        session,
        org_id=agent_auth.org.id,
        data=await file.read(),
        filename=file.filename or "upload",
        media_type=file.content_type or "",
        project_id=provider_id,
        declared_kind=document_type,
    )
    return await _process(session, agent_auth, result, extractor)


async def _process(
    session: Session,
    agent_auth: AgentAuth,
    result: ingestion.Ingested,
    extractor: str | None,
) -> dict:
    """Commit the ingest, run the pipeline, and report both.

    Processing runs inline (ADR-030). It is the one thing here that would look
    different with a worker, and the stage checkpoints are what keep that a
    change of caller rather than a rewrite.
    """
    session.commit()
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="documentation.uploaded",
        summary=f"Document '{result.document.filename}' ingested",
        target_type="document",
        target_id=str(result.document.id),
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()

    job = await jobs.run_job(session, result.job, extractor_id=extractor)
    session.refresh(result.document)
    return envelope(
        {
            "job_id": str(job.id),
            "job": _serialize_job(job),
            "document": _serialize_document(result.document),
            "deduplicated": result.deduplicated,
        }
    )


@router.get("/jobs/{job_id}")
def get_job(
    job_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    job = session.get(DocumentJob, job_id)
    if job is None or job.org_id != agent_auth.org.id:
        raise NotFoundError(f"Job {job_id} was not found.")
    return envelope(_serialize_job(job))


@router.post("/documents/{document_id}/reprocess")
async def reprocess(
    document_id: uuid.UUID,
    extractor: str | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Run the pipeline again over a document already in the platform.

    A fresh job, not a resumed one: reprocessing is what you do when the
    extractor has changed, and reusing the old job's checkpoints would skip
    precisely the stages you wanted to re-run.
    """
    ensure_agent_can(session, agent_auth, WRITE)
    document = _load_document(session, document_id, agent_auth.org.id)
    job = DocumentJob(org_id=document.org_id, document_id=document.id)
    session.add(job)
    session.commit()
    job = await jobs.run_job(session, job, extractor_id=extractor)
    session.refresh(document)
    return envelope({"job": _serialize_job(job), "document": _serialize_document(document)})


@router.get("/documents")
def list_documents(
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    document_type: str | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    statement = select(Document).where(Document.org_id == agent_auth.org.id)
    if project_id:
        statement = statement.where(Document.project_id == project_id)
    if status:
        statement = statement.where(Document.status == status)
    if document_type:
        statement = statement.where(Document.document_type == document_type)
    rows = session.exec(statement.order_by(col(Document.uploaded_at).desc())).all()
    return envelope([_serialize_document(row) for row in rows])


@router.get("/documents/{document_id}")
def get_document(
    document_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    document = _load_document(session, document_id, agent_auth.org.id)
    counts = {
        "chunks": _count(session, DocumentChunk, document.id),
        "rules": _count(session, BusinessRule, document.id),
        "workflows": _count(session, DocWorkflow, document.id),
        "terms": _count(session, GlossaryTerm, document.id),
        "embeddings": _count(session, ChunkEmbedding, document.id),
    }
    latest = session.exec(
        select(DocumentJob)
        .where(DocumentJob.document_id == document.id)
        .order_by(col(DocumentJob.created_at).desc())
    ).first()
    return envelope(
        {
            **_serialize_document(document),
            "counts": counts,
            "latest_job": _serialize_job(latest) if latest else None,
        }
    )


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> None:
    """Remove a document and everything derived from it.

    Every derived row goes: a business rule whose source document is gone is a
    claim with no citation, which is the one thing this service must never
    hold.
    """
    ensure_agent_can(session, agent_auth, WRITE)
    document = _load_document(session, document_id, agent_auth.org.id)

    node_ids = [
        node.id
        for node in session.exec(
            select(KnowledgeNode).where(KnowledgeNode.document_id == document.id)
        ).all()
    ]
    if node_ids:
        session.exec(
            delete(KnowledgeEdge).where(
                col(KnowledgeEdge.source_node_id).in_(node_ids)
                | col(KnowledgeEdge.target_node_id).in_(node_ids)
            )
        )
    session.exec(delete(KnowledgeNode).where(KnowledgeNode.document_id == document.id))
    for model in (ChunkEmbedding, BusinessRule, DocWorkflow, GlossaryTerm):
        session.exec(delete(model).where(model.document_id == document.id))
    session.exec(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
    session.exec(delete(DocumentJob).where(DocumentJob.document_id == document.id))

    sha = document.sha256
    session.delete(document)
    record_audit(
        session,
        org_id=agent_auth.org.id,
        action="documentation.deleted",
        summary=f"Document '{document.filename}' deleted",
        target_type="document",
        target_id=str(document_id),
        **actor_from_agent_auth(agent_auth),
    )
    session.commit()
    # Only after the row is gone: an orphaned blob is recoverable waste, a row
    # pointing at a deleted blob is a broken citation.
    storage.get_storage().delete(sha)


@router.get("/documents/{document_id}/chunks")
def list_chunks(
    document_id: uuid.UUID,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    document = _load_document(session, document_id, agent_auth.org.id)
    rows = session.exec(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document.id)
        .order_by(col(DocumentChunk.position))
    ).all()
    return envelope(
        [
            {
                "id": str(row.id),
                "position": row.position,
                "section_path": row.section_path,
                "element_type": row.element_type,
                "page": row.page,
                "start_offset": row.start_offset,
                "end_offset": row.end_offset,
                "token_estimate": row.token_estimate,
                "text": row.text,
            }
            for row in rows
        ]
    )


@router.get("/rules")
def list_rules(
    document_id: uuid.UUID | None = None,
    rule_type: str | None = None,
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Extracted business rules, each with the sentence it came from."""
    ensure_agent_can(session, agent_auth, READ)
    statement = select(BusinessRule).where(
        BusinessRule.org_id == agent_auth.org.id,
        BusinessRule.confidence >= min_confidence,
    )
    if document_id:
        statement = statement.where(BusinessRule.document_id == document_id)
    if rule_type:
        statement = statement.where(BusinessRule.rule_type == rule_type)
    rows = session.exec(statement.order_by(col(BusinessRule.confidence).desc())).all()
    return envelope(
        [
            {
                "id": str(row.id),
                "rule_type": row.rule_type,
                "condition": row.condition,
                "action": row.action,
                "confidence": row.confidence,
                "extractor_version": row.extractor_version,
                "signals": json.loads(row.signals_json or "[]"),
                "citation": _citation(row),
            }
            for row in rows
        ]
    )


@router.get("/workflows")
def list_workflows(
    document_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    statement = select(DocWorkflow).where(DocWorkflow.org_id == agent_auth.org.id)
    if document_id:
        statement = statement.where(DocWorkflow.document_id == document_id)
    rows = session.exec(statement.order_by(col(DocWorkflow.confidence).desc())).all()
    return envelope(
        [
            {
                "id": str(row.id),
                "name": row.name,
                "description": row.description,
                "nodes": json.loads(row.nodes_json or "[]"),
                "edges": json.loads(row.edges_json or "[]"),
                "operation_refs": json.loads(row.operation_refs_json or "[]"),
                "confidence": row.confidence,
                "extractor_version": row.extractor_version,
                "citation": _citation(row),
            }
            for row in rows
        ]
    )


@router.get("/glossary")
def list_glossary(
    document_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    ensure_agent_can(session, agent_auth, READ)
    statement = select(GlossaryTerm).where(GlossaryTerm.org_id == agent_auth.org.id)
    if document_id:
        statement = statement.where(GlossaryTerm.document_id == document_id)
    rows = session.exec(statement.order_by(col(GlossaryTerm.term))).all()
    return envelope(
        [
            {
                "id": str(row.id),
                "term": row.term,
                "definition": row.definition,
                "aliases": json.loads(row.aliases_json or "[]"),
                "confidence": row.confidence,
                "extractor_version": row.extractor_version,
                "citation": _citation(row),
            }
            for row in rows
        ]
    )


@router.get("/graph")
def get_graph(
    entity_type: str | None = None,
    entity_key: str | None = None,
    depth: int = Query(default=1, ge=1, le=3),
    document_id: uuid.UUID | None = None,
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """The knowledge graph, either whole (per document) or around one node."""
    ensure_agent_can(session, agent_auth, READ)
    if entity_type and entity_key:
        result = neighbours(
            session,
            org_id=agent_auth.org.id,
            entity_type=entity_type,
            entity_key=entity_key,
            depth=depth,
        )
        return envelope(result)

    statement = select(KnowledgeNode).where(KnowledgeNode.org_id == agent_auth.org.id)
    if document_id:
        statement = statement.where(KnowledgeNode.document_id == document_id)
    nodes = session.exec(statement).all()
    node_ids = {node.id for node in nodes}
    edges = [
        edge
        for edge in session.exec(
            select(KnowledgeEdge).where(KnowledgeEdge.org_id == agent_auth.org.id)
        ).all()
        if edge.source_node_id in node_ids and edge.target_node_id in node_ids
    ]
    return envelope(
        {
            "nodes": [
                {
                    "id": str(node.id),
                    "entity_type": node.entity_type,
                    "entity_key": node.entity_key,
                    "label": node.label,
                    "properties": json.loads(node.properties_json or "{}"),
                    "document_id": str(node.document_id) if node.document_id else None,
                }
                for node in nodes
            ],
            "edges": [
                {
                    "id": str(edge.id),
                    "source": str(edge.source_node_id),
                    "target": str(edge.target_node_id),
                    "relationship": edge.relationship,
                    "properties": json.loads(edge.properties_json or "{}"),
                }
                for edge in edges
            ],
        }
    )


@router.get("/search")
async def search(
    q: str = Query(min_length=1, max_length=1000),
    document_id: uuid.UUID | None = None,
    limit: int = Query(default=10, ge=1, le=100),
    session: Session = Depends(get_session),
    agent_auth: AgentAuth = Depends(get_agent_auth),
) -> dict:
    """Search this tenant's documentation.

    The response says which mode it ran in. When no embedding provider is
    configured this is lexical search, and it says so rather than presenting
    BM25 results under a name that implies more.
    """
    ensure_agent_can(session, agent_auth, READ)
    result = await retrieval.search(
        session,
        org_id=agent_auth.org.id,
        query=q,
        document_id=document_id,
        limit=limit,
    )
    return envelope(result.as_dict())


def _count(session: Session, model, document_id: uuid.UUID) -> int:
    return session.exec(
        select(func.count()).select_from(model).where(model.document_id == document_id)
    ).one()
