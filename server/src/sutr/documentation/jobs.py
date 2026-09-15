"""The documentation pipeline, stage by stage, with checkpoints.

    Document → Parse → Chunk → Extract → Graph → Embed

LLD §3.5 requires a *checkpoint per stage*, so a failure resumes rather than
restarting — parsing a 300-page PDF twice because the graph step failed would
be absurd. Completed stages are recorded on the job, and a re-run skips them.

The failure rules are the LLD's, and they are the reason `partial` exists as a
state distinct from `failed`:

- **OCR cannot read a page** → flag the page, continue.
- **The graph backend is unavailable** → continue without the graph, warn.
- **No embedding provider** → continue without vectors, warn.

Only parsing failing outright fails the job: a document nobody can read has
nothing downstream to do. Everything after it degrades.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session, col, delete, select

from sutr.config import settings
from sutr.documentation import events as doc_events
from sutr.documentation import graph as graph_module
from sutr.documentation.chunking import chunk_elements
from sutr.documentation.classification import classify
from sutr.documentation.extraction import get_extractor
from sutr.documentation.parsing import (
    TITLE,
    ParseError,
    ParserUnavailableError,
    parse_document,
)
from sutr.documentation.storage import StorageError, get_storage
from sutr.models.business_rule import BusinessRule
from sutr.models.doc_workflow import DocWorkflow, GlossaryTerm
from sutr.models.document import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PROCESSED,
    STATUS_PROCESSING,
    Document,
)
from sutr.models.document_chunk import DocumentChunk
from sutr.models.document_job import (
    STAGE_CHUNK,
    STAGE_EMBED,
    STAGE_EXTRACT,
    STAGE_GRAPH,
    STAGE_PARSE,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    DocumentJob,
)
from sutr.observability import log_context

logger = logging.getLogger(__name__)


@dataclass
class StageOutcome:
    name: str
    status: str  # "ok" | "skipped" | "failed"
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "error": self.error,
        }


class _Clock:
    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        return False

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)


async def run_job(
    session: Session, job: DocumentJob, *, extractor_id: str | None = None
) -> DocumentJob:
    """Run every stage that has not already completed.

    Idempotent by design: re-running a job re-does only what is missing, which
    is what makes "resume after a failure" the same code path as "run".
    """
    document = session.get(Document, job.document_id)
    if document is None:
        return _fail(session, job, "document_missing", "The document no longer exists.")

    with log_context.bound(tenant_id=str(job.org_id), provider_id=str(document.id)):
        completed = set(json.loads(job.completed_stages_json or "[]"))
        outcomes = (
            [StageOutcome(**entry) for entry in json.loads(job.stage_results_json or "[]")]
            if job.stage_results_json
            else []
        )
        degradations: list[dict] = json.loads(job.degradations_json or "[]")

        extractor = get_extractor(extractor_id)
        job.extractor_version = extractor.version
        job.status = STATUS_RUNNING
        job.attempts += 1
        job.started_at = job.started_at or datetime.utcnow()
        document.status = STATUS_PROCESSING
        session.add_all([job, document])
        session.commit()

        doc_events.uploaded(
            session,
            org_id=job.org_id,
            document_id=document.id,
            job_id=job.id,
            filename=document.filename,
            kind=document.kind,
        )
        session.commit()

        # ── parse ────────────────────────────────────────────────────────────
        parse_result = None
        if STAGE_PARSE not in completed:
            job.current_stage = STAGE_PARSE
            session.add(job)
            session.commit()
            with _Clock() as clock:
                try:
                    parse_result = parse_document(
                        document.kind, _content(document), allow_ocr=settings.document_ocr_enabled
                    )
                except ParserUnavailableError as exc:
                    return _fail(
                        session,
                        job,
                        "parser_unavailable",
                        f"{exc.message} {exc.install_hint}".strip(),
                        outcomes
                        + [StageOutcome(STAGE_PARSE, "failed", clock.ms, error=exc.message)],
                    )
                except StorageError as exc:
                    return _fail(
                        session,
                        job,
                        "content_unavailable",
                        str(exc),
                        outcomes + [StageOutcome(STAGE_PARSE, "failed", clock.ms, error=str(exc))],
                    )
                except ParseError as exc:
                    return _fail(
                        session,
                        job,
                        exc.code,
                        exc.message,
                        outcomes
                        + [StageOutcome(STAGE_PARSE, "failed", clock.ms, error=exc.message)],
                    )
            degradations += parse_result.degradations
            # Classification rides on the parse stage: it needs the headings,
            # and it is far too cheap to be worth a checkpoint of its own.
            classification = classify(
                filename=document.filename,
                headings=[
                    element.text
                    for element in parse_result.elements
                    if element.element_type == TITLE
                ],
                text=parse_result.text,
            )
            document.document_type = classification.document_type
            document.classification_json = json.dumps(classification.as_dict())
            session.add(document)
            outcomes.append(
                StageOutcome(
                    STAGE_PARSE,
                    "ok",
                    clock.ms,
                    detail={
                        "parser": parse_result.parser,
                        "elements": len(parse_result.elements),
                        "pages": parse_result.page_count,
                        "characters": len(parse_result.text),
                        "document_type": classification.document_type,
                        "classification_confidence": classification.confidence,
                    },
                )
            )
            completed.add(STAGE_PARSE)
            _checkpoint(session, job, completed, outcomes, degradations)

            doc_events.parsing_completed(
                session,
                org_id=job.org_id,
                document_id=document.id,
                parser=parse_result.parser,
                elements=len(parse_result.elements),
                pages=parse_result.page_count,
                degradations=len(parse_result.degradations),
            )
            session.commit()

        # ── chunk ────────────────────────────────────────────────────────────
        if STAGE_CHUNK not in completed:
            if parse_result is None:
                # Resuming a job whose parse completed in a previous run: the
                # elements were not persisted, so parsing repeats. Cheap
                # relative to extraction, and simpler than a second blob.
                parse_result = parse_document(
                    document.kind, _content(document), allow_ocr=settings.document_ocr_enabled
                )
            job.current_stage = STAGE_CHUNK
            session.add(job)
            session.commit()
            with _Clock() as clock:
                chunks = chunk_elements(parse_result.elements)
                session.exec(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
                for chunk in chunks:
                    session.add(
                        DocumentChunk(
                            org_id=job.org_id,
                            document_id=document.id,
                            position=chunk.position,
                            section_path=chunk.section_path,
                            element_type=chunk.element_type,
                            text=chunk.text,
                            start_offset=chunk.start_offset,
                            end_offset=chunk.end_offset,
                            page=chunk.page,
                            token_estimate=chunk.token_estimate,
                        )
                    )
                session.commit()
            outcomes.append(
                StageOutcome(STAGE_CHUNK, "ok", clock.ms, detail={"chunks": len(chunks)})
            )
            completed.add(STAGE_CHUNK)
            _checkpoint(session, job, completed, outcomes, degradations)

        stored_chunks = list(
            session.exec(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document.id)
                .order_by(col(DocumentChunk.position))
            ).all()
        )

        # ── extract ──────────────────────────────────────────────────────────
        if STAGE_EXTRACT not in completed:
            job.current_stage = STAGE_EXTRACT
            session.add(job)
            session.commit()
            with _Clock() as clock:
                extraction = extractor.extract(stored_chunks)
                degradations += extraction.degradations
                _persist_extraction(session, job, document, extraction, extractor.version)
                session.commit()
            outcomes.append(
                StageOutcome(
                    STAGE_EXTRACT,
                    "ok",
                    clock.ms,
                    detail={
                        "extractor": extractor.id,
                        "version": extractor.version,
                        "rules": len(extraction.rules),
                        "workflows": len(extraction.workflows),
                        "terms": len(extraction.terms),
                    },
                )
            )
            completed.add(STAGE_EXTRACT)
            _checkpoint(session, job, completed, outcomes, degradations)

            doc_events.knowledge_extracted(
                session,
                org_id=job.org_id,
                document_id=document.id,
                rules=len(extraction.rules),
                workflows=len(extraction.workflows),
                terms=len(extraction.terms),
                extractor_version=extractor.version,
            )
            session.commit()

        # ── graph ────────────────────────────────────────────────────────────
        if STAGE_GRAPH not in completed:
            job.current_stage = STAGE_GRAPH
            session.add(job)
            session.commit()
            with _Clock() as clock:
                try:
                    summary = graph_module.build_graph(
                        session,
                        org_id=job.org_id,
                        document_id=document.id,
                        document_name=document.filename,
                        rules=_load_rules(session, document.id),
                        workflows=_load_workflows(session, document.id),
                        terms=_load_terms(session, document.id),
                    )
                    session.commit()
                    outcomes.append(
                        StageOutcome(
                            STAGE_GRAPH,
                            "ok",
                            clock.ms,
                            detail={
                                "nodes": summary.nodes_created,
                                "edges": summary.edges_created,
                            },
                        )
                    )
                except Exception as exc:
                    # LLD §3.5: graph failure ⇒ continue without graph + warning.
                    session.rollback()
                    logger.warning("graph build failed for %s", document.id, exc_info=True)
                    degradations.append(
                        {
                            "code": "graph_unavailable",
                            "message": f"The knowledge graph could not be built: {exc}",
                            "stage": STAGE_GRAPH,
                        }
                    )
                    outcomes.append(
                        StageOutcome(STAGE_GRAPH, "skipped", clock.ms, error=str(exc)[:300])
                    )
            completed.add(STAGE_GRAPH)
            _checkpoint(session, job, completed, outcomes, degradations)

        # ── embed ────────────────────────────────────────────────────────────
        if STAGE_EMBED not in completed:
            from sutr.documentation.embeddings import embed_document

            job.current_stage = STAGE_EMBED
            session.add(job)
            session.commit()
            with _Clock() as clock:
                summary = await embed_document(session, org_id=job.org_id, document_id=document.id)
                session.commit()
            if summary.get("skipped"):
                degradations.append(
                    {
                        "code": "embeddings_unavailable",
                        "message": summary.get(
                            "unavailable_reason", "No embeddings were generated."
                        ),
                        "stage": STAGE_EMBED,
                    }
                )
                outcomes.append(StageOutcome(STAGE_EMBED, "skipped", clock.ms, detail=summary))
            else:
                outcomes.append(StageOutcome(STAGE_EMBED, "ok", clock.ms, detail=summary))
                doc_events.embeddings_generated(
                    session,
                    org_id=job.org_id,
                    document_id=document.id,
                    generated=summary.get("generated", 0),
                    model=summary.get("model"),
                )
            completed.add(STAGE_EMBED)
            _checkpoint(session, job, completed, outcomes, degradations)

        # ── finish ───────────────────────────────────────────────────────────
        job.status = STATUS_COMPLETED
        job.current_stage = None
        job.finished_at = datetime.utcnow()
        document.status = STATUS_PARTIAL if degradations else STATUS_PROCESSED
        document.degradations_json = json.dumps(degradations)
        document.processed_at = datetime.utcnow()
        session.add_all([job, document])

        doc_events.processed(
            session,
            org_id=job.org_id,
            document_id=document.id,
            job_id=job.id,
            status=document.status,
            degradations=len(degradations),
        )
        session.commit()
        session.refresh(job)
        return job


def _content(document: Document) -> bytes:
    """The original bytes, from whichever backend holds them."""
    return get_storage().get(document.sha256, document.content or None)


def _checkpoint(
    session: Session,
    job: DocumentJob,
    completed: set,
    outcomes: list[StageOutcome],
    degradations: list[dict],
) -> None:
    """Record the resume point. The LLD's per-stage checkpoint."""
    from sutr.models.document_job import STAGES

    job.completed_stages_json = json.dumps([stage for stage in STAGES if stage in completed])
    job.stage_results_json = json.dumps([outcome.as_dict() for outcome in outcomes])
    job.degradations_json = json.dumps(degradations)
    session.add(job)
    session.commit()


def _fail(
    session: Session,
    job: DocumentJob,
    code: str,
    message: str,
    outcomes: list[StageOutcome] | None = None,
) -> DocumentJob:
    from sutr.models.document_job import STATUS_FAILED as JOB_FAILED

    job.status = JOB_FAILED
    job.error_code = code
    job.error_message = message[:1000]
    job.finished_at = datetime.utcnow()
    if outcomes is not None:
        job.stage_results_json = json.dumps([outcome.as_dict() for outcome in outcomes])
    session.add(job)

    document = session.get(Document, job.document_id)
    if document is not None:
        document.status = STATUS_FAILED
        session.add(document)

    doc_events.failed(
        session,
        org_id=job.org_id,
        document_id=job.document_id,
        job_id=job.id,
        error_code=code,
        error_message=message,
    )
    session.commit()
    session.refresh(job)
    return job


def _persist_extraction(
    session: Session, job: DocumentJob, document: Document, extraction, version: str
) -> None:
    """Replace this document's extracted facts.

    Replaced rather than appended: re-running extraction on the same document
    should produce the same set, not a second copy of it.
    """
    session.exec(delete(BusinessRule).where(BusinessRule.document_id == document.id))
    session.exec(delete(DocWorkflow).where(DocWorkflow.document_id == document.id))
    session.exec(delete(GlossaryTerm).where(GlossaryTerm.document_id == document.id))

    for rule in extraction.rules:
        session.add(
            BusinessRule(
                org_id=job.org_id,
                document_id=document.id,
                chunk_id=_chunk_uuid(rule.citation.chunk_id),
                job_id=job.id,
                rule_type=rule.rule_type,
                condition=rule.condition,
                action=rule.action,
                source_document=document.filename,
                source_location=rule.citation.location,
                source_text=rule.citation.text,
                start_offset=rule.citation.start_offset,
                end_offset=rule.citation.end_offset,
                page=rule.citation.page,
                confidence=rule.confidence,
                extractor_version=version,
                signals_json=json.dumps(rule.signals),
            )
        )

    for workflow in extraction.workflows:
        session.add(
            DocWorkflow(
                org_id=job.org_id,
                document_id=document.id,
                chunk_id=_chunk_uuid(workflow.citation.chunk_id),
                job_id=job.id,
                name=workflow.name,
                description=workflow.description,
                nodes_json=json.dumps(workflow.nodes),
                edges_json=json.dumps(workflow.edges),
                operation_refs_json=json.dumps(
                    [node["operation_id"] for node in workflow.nodes if node.get("operation_id")]
                ),
                source_document=document.filename,
                source_location=workflow.citation.location,
                source_text=workflow.citation.text,
                confidence=workflow.confidence,
                extractor_version=version,
            )
        )

    for term in extraction.terms:
        session.add(
            GlossaryTerm(
                org_id=job.org_id,
                document_id=document.id,
                chunk_id=_chunk_uuid(term.citation.chunk_id),
                job_id=job.id,
                term=term.term,
                definition=term.definition,
                aliases_json=json.dumps(term.aliases),
                source_document=document.filename,
                source_location=term.citation.location,
                source_text=term.citation.text,
                confidence=term.confidence,
                extractor_version=version,
            )
        )


def _chunk_uuid(value) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def _load_rules(session: Session, document_id: uuid.UUID):
    from sutr.documentation.extraction.base import Citation, ExtractedRule

    rows = session.exec(select(BusinessRule).where(BusinessRule.document_id == document_id)).all()
    return [
        ExtractedRule(
            rule_type=row.rule_type,
            condition=row.condition,
            action=row.action,
            citation=Citation(
                start_offset=row.start_offset,
                end_offset=row.end_offset,
                page=row.page,
                text=row.source_text,
                section_path=row.source_location,
            ),
            confidence=row.confidence,
        )
        for row in rows
    ]


def _load_workflows(session: Session, document_id: uuid.UUID):
    from sutr.documentation.extraction.base import Citation, ExtractedWorkflow

    rows = session.exec(select(DocWorkflow).where(DocWorkflow.document_id == document_id)).all()
    return [
        ExtractedWorkflow(
            name=row.name,
            nodes=json.loads(row.nodes_json or "[]"),
            edges=json.loads(row.edges_json or "[]"),
            citation=Citation(text=row.source_text, section_path=row.source_location),
            description=row.description,
            confidence=row.confidence,
        )
        for row in rows
    ]


def _load_terms(session: Session, document_id: uuid.UUID):
    from sutr.documentation.extraction.base import Citation, ExtractedTerm

    rows = session.exec(select(GlossaryTerm).where(GlossaryTerm.document_id == document_id)).all()
    return [
        ExtractedTerm(
            term=row.term,
            definition=row.definition,
            citation=Citation(text=row.source_text, section_path=row.source_location),
            confidence=row.confidence,
        )
        for row in rows
    ]
