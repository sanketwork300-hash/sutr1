"""The events the documentation pipeline announces.

One module rather than `events.publish(...)` scattered through the job runner,
for a specific reason: these five events are a *contract* — the LLD names them
and other services will consume them — and a contract that is assembled inline
at six call sites drifts payload by payload.

    documentation.uploaded
      → parsing.completed
        → knowledge.extracted
          → embeddings.generated      (only when a provider is configured)
            → documentation.processed
    documentation.failed              (terminal, replaces the rest)

Every publish joins the caller's transaction (`events.outbox`), so an event is
never announced for a state change that later rolls back.
"""

import uuid

from sqlmodel import Session

from sutr import events
from sutr.events import topics

PRODUCER = "documentation"


def _publish(
    session: Session, event_type: str, org_id: uuid.UUID, document_id: uuid.UUID, **payload
) -> None:
    events.publish(
        session,
        event_type,
        tenant_id=org_id,
        resource_id=str(document_id),
        producer=PRODUCER,
        payload={"document_id": str(document_id), **payload},
    )


def uploaded(
    session: Session,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    filename: str,
    kind: str,
) -> None:
    _publish(
        session,
        topics.DOCUMENTATION_UPLOADED,
        org_id,
        document_id,
        job_id=str(job_id),
        filename=filename,
        kind=kind,
    )


def parsing_completed(
    session: Session,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    parser: str,
    elements: int,
    pages: int | None,
    degradations: int,
) -> None:
    _publish(
        session,
        topics.PARSING_COMPLETED,
        org_id,
        document_id,
        parser=parser,
        elements=elements,
        pages=pages,
        degradations=degradations,
    )


def knowledge_extracted(
    session: Session,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    rules: int,
    workflows: int,
    terms: int,
    extractor_version: str,
) -> None:
    _publish(
        session,
        topics.KNOWLEDGE_EXTRACTED,
        org_id,
        document_id,
        rules=rules,
        workflows=workflows,
        terms=terms,
        extractor_version=extractor_version,
    )


def embeddings_generated(
    session: Session,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    generated: int,
    model: str | None,
) -> None:
    _publish(
        session,
        topics.EMBEDDINGS_GENERATED,
        org_id,
        document_id,
        generated=generated,
        model=model,
    )


def processed(
    session: Session,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    status: str,
    degradations: int,
) -> None:
    _publish(
        session,
        topics.DOCUMENTATION_PROCESSED,
        org_id,
        document_id,
        job_id=str(job_id),
        status=status,
        degradations=degradations,
    )


def failed(
    session: Session,
    *,
    org_id: uuid.UUID,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    error_code: str,
    error_message: str,
) -> None:
    _publish(
        session,
        topics.DOCUMENTATION_FAILED,
        org_id,
        document_id,
        job_id=str(job_id),
        error_code=error_code,
        # Truncated: an event payload is a notification, not a log record.
        error_message=error_message[:300],
    )
