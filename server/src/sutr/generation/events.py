"""The events a generation run announces.

LLD §3.6 names the chain:

    metadata.generated
      → generation.started
        → mcp.generated        (the LLD's prose calls this runtime.generated;
          / generation.failed   its Kafka topic table, p. 11, calls it
            → validation.completed   mcp.generated — the topic name wins,
                                      since that is what a consumer subscribes
                                      to)

Every event of one run carries the same `generation_id` as its resource id.
Ordering in the bus is guaranteed per partition and the partition is chosen
from that id (`events/topics.py`), so a run's five events stay in order even
when several runs are in flight. The artifact id is *not* used for this: it
does not exist until the run is nearly over, and three of the five events
happen before then.

Every publish joins the caller's transaction, so nothing is announced for a
build that later rolls back.
"""

import uuid
from typing import Any

from sqlmodel import Session

from sutr import events
from sutr.events import topics

PRODUCER = "mcp_generator"


def _publish(
    session: Session,
    event_type: str,
    org_id: uuid.UUID,
    generation_id: uuid.UUID,
    **payload: Any,
) -> None:
    events.publish(
        session,
        event_type,
        tenant_id=org_id,
        resource_id=str(generation_id),
        producer=PRODUCER,
        payload={"generation_id": str(generation_id), **payload},
    )


def metadata_generated(
    session: Session,
    *,
    org_id: uuid.UUID,
    generation_id: uuid.UUID,
    project_id: uuid.UUID | None,
    tool_count: int,
    knowledge: dict[str, Any],
) -> None:
    """The manifest and the knowledge that will go into the build are ready."""
    _publish(
        session,
        topics.METADATA_GENERATED,
        org_id,
        generation_id,
        project_id=str(project_id) if project_id else None,
        tool_count=tool_count,
        knowledge=knowledge,
    )


def started(
    session: Session,
    *,
    org_id: uuid.UUID,
    generation_id: uuid.UUID,
    project_id: uuid.UUID | None,
    runtime: str,
    template_version: str,
) -> None:
    _publish(
        session,
        topics.GENERATION_STARTED,
        org_id,
        generation_id,
        project_id=str(project_id) if project_id else None,
        runtime=runtime,
        template_version=template_version,
    )


def generated(
    session: Session,
    *,
    org_id: uuid.UUID,
    generation_id: uuid.UUID,
    artifact_id: uuid.UUID,
    build_hash: str,
    package_sha256: str,
    tool_count: int,
    signed: bool,
    reused: bool,
) -> None:
    """A runtime artifact exists.

    `reused` says the build was byte-identical to one already stored and no new
    artifact was written. A consumer that treats every `mcp.generated` as a new
    version would otherwise create a version per rebuild.
    """
    _publish(
        session,
        topics.MCP_GENERATED,
        org_id,
        generation_id,
        artifact_id=str(artifact_id),
        build_hash=build_hash,
        package_sha256=package_sha256,
        tool_count=tool_count,
        signed=signed,
        reused=reused,
    )


def failed(
    session: Session,
    *,
    org_id: uuid.UUID,
    generation_id: uuid.UUID,
    stage: str,
    error_code: str,
    error_message: str,
) -> None:
    _publish(
        session,
        topics.GENERATION_FAILED,
        org_id,
        generation_id,
        stage=stage,
        error_code=error_code,
        # Truncated: an event payload is a notification, not a log record.
        error_message=error_message[:300],
    )


def validation_completed(
    session: Session,
    *,
    org_id: uuid.UUID,
    generation_id: uuid.UUID,
    artifact_id: uuid.UUID | None,
    validated: bool,
    failed_checks: list[str],
    blocked_checks: list[str],
) -> None:
    _publish(
        session,
        topics.VALIDATION_COMPLETED,
        org_id,
        generation_id,
        artifact_id=str(artifact_id) if artifact_id else None,
        validated=validated,
        failed_checks=failed_checks,
        blocked_checks=blocked_checks,
    )
