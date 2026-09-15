"""Usage metering writer.

Like `record_audit`, `record_usage` adds the row to the caller's session
WITHOUT committing, so a metered event can never be recorded for work that
rolled back — and billable work can never commit without being metered.

**No price is written here, and none can be.** LLD §5.1: *"Metering captures
execution facts only — prices are evaluated at billing time, never during
execution."* This module takes no amount, imports nothing from `billing/`, and
a test asserts both. That is what makes a repriced tool not retroactively
repriced: the fact and its price are recorded at different times by different
code.

`invocation_id` is the dedupe key §5.1 asks for. A replayed event with an id
already seen for this tenant returns the existing row instead of writing a
second one, so an at-least-once delivery cannot become a double charge.
"""

import json
import uuid

from sqlmodel import Session

from sutr import events
from sutr.config import settings
from sutr.events import topics
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
    invocation_id: str | None = None,
    region: str | None = None,
    payload_bytes: int | None = None,
    tokens: int | None = None,
    provider_org_id: uuid.UUID | None = None,
    tool_id: uuid.UUID | None = None,
    pricing_context: dict | None = None,
) -> UsageEvent:
    if invocation_id:
        existing = find_by_invocation(session, org_id=org_id, invocation_id=invocation_id)
        if existing is not None:
            # At-least-once delivery is the norm, so a replay is expected and
            # must be free. Returning the original rather than raising keeps
            # the caller's path identical whether or not it is the first time.
            return existing
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
        invocation_id=invocation_id,
        region=region or (settings.region or None),
        payload_bytes=payload_bytes,
        tokens=tokens,
        provider_org_id=provider_org_id,
        tool_id=tool_id,
        # Dimensions billing will need. Deliberately not a price: naming a plan
        # here would evaluate pricing during execution.
        pricing_context_json=json.dumps(pricing_context or {}, sort_keys=True),
    )
    session.add(event)
    return event


def find_by_invocation(
    session: Session, *, org_id: uuid.UUID, invocation_id: str
) -> UsageEvent | None:
    """The event already recorded for this invocation, if there is one."""
    from sqlmodel import select

    return session.exec(
        select(UsageEvent)
        .where(UsageEvent.org_id == org_id)
        .where(UsageEvent.invocation_id == invocation_id)
    ).first()


def record_tool_call(session: Session, ctx, integration_id: str, tool_name: str, outcome) -> None:
    """Meter one tool execution from the canonical pipeline's context+outcome."""
    event = record_usage(
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
    # The LLD's billing flow is Gateway → usage.recorded → Billing (§5.6).
    # Off by default: the ledger row above is already durable and
    # authoritative, and nothing consumes the event yet, so emitting one would
    # double the write volume of every invocation for no reader.
    if settings.emit_usage_events:
        events.publish(
            session,
            topics.USAGE_RECORDED,
            tenant_id=ctx.org_id,
            resource_id=str(ctx.org_id),
            producer="metering",
            payload={
                "integration_id": integration_id,
                "tool_name": tool_name,
                "source": ctx.source,
                "outcome": outcome.outcome,
                "duration_ms": outcome.duration_ms,
                "quantity": event.quantity,
            },
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
