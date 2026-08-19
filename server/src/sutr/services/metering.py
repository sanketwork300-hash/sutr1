"""Usage metering writer.

Like `record_audit`, `record_usage` adds the row to the caller's session
WITHOUT committing, so a metered event can never be recorded for work that
rolled back — and billable work can never commit without being metered.
"""

import json
import uuid

from sqlmodel import Session

from sutr.models.usage_event import KIND_DEPLOYMENT_RUNTIME, KIND_TOOL_CALL, UsageEvent


def record_usage(
    session: Session,
    *,
    org_id: uuid.UUID,
    kind: str,
    quantity: int = 1,
    integration_id: str | None = None,
    tool_name: str | None = None,
    source: str | None = None,
    outcome: str | None = None,
    duration_ms: int | None = None,
    user_id: uuid.UUID | None = None,
    api_key_prefix: str | None = None,
    metadata: dict | None = None,
) -> UsageEvent:
    event = UsageEvent(
        org_id=org_id,
        kind=kind,
        quantity=quantity,
        integration_id=integration_id,
        tool_name=tool_name,
        source=source,
        outcome=outcome,
        duration_ms=duration_ms,
        user_id=user_id,
        api_key_prefix=api_key_prefix,
        metadata_json=json.dumps(metadata or {}),
    )
    session.add(event)
    return event


def record_tool_call(session: Session, ctx, integration_id: str, tool_name: str, outcome) -> None:
    """Meter one tool execution from the canonical pipeline's context+outcome."""
    record_usage(
        session,
        org_id=ctx.org_id,
        kind=KIND_TOOL_CALL,
        integration_id=integration_id,
        tool_name=tool_name,
        source=ctx.source,
        outcome=outcome.outcome,
        duration_ms=outcome.duration_ms,
        user_id=ctx.impersonator_user_id or None,
        api_key_prefix=ctx.api_key_prefix,
    )


def record_deployment_runtime(
    session: Session,
    *,
    org_id: uuid.UUID,
    deployment_id: uuid.UUID,
    minutes: int,
) -> None:
    record_usage(
        session,
        org_id=org_id,
        kind=KIND_DEPLOYMENT_RUNTIME,
        quantity=minutes,
        source="system",
        metadata={"deployment_id": str(deployment_id)},
    )
