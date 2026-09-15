"""The event envelope — fixed before anything is emitted.

ESDS LLD §5.6 requires every event to be an immutable fact carrying
`event_id`, `event_type`, `event_version`, `timestamp`, `correlation_id`,
`tenant_id`, `resource_id`, `producer` and `payload`. That list is not
negotiable and not extensible per-event: a consumer reads the envelope without
knowing the type, which only works if the envelope is the same shape every
time.

Two consequences worth stating:

- **Events are facts, never commands.** `tool.published` says something
  happened. There is no `publish.tool` — a command would make the producer
  responsible for the consumer's behaviour, which is the coupling events exist
  to remove.
- **The correlation id is inherited, not minted.** An event emitted while
  handling a request carries that request's correlation id, so one logical
  operation stays traceable across every stage that reacts to it.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from sutr.request_context import get_correlation_id

# The envelope's own version, distinct from an event type's version. Bumped
# only if the envelope's required fields change, which should be almost never.
ENVELOPE_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EventEnvelope(BaseModel):
    """One immutable fact."""

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    # Version of *this event type's* payload. A consumer that understands v1
    # must keep working when v2 appears, so changes are additive and the
    # version says which shape to expect.
    event_version: int = 1
    timestamp: datetime = Field(default_factory=_now)
    correlation_id: str | None = None
    # The organization the fact is about. None only for platform-wide facts.
    tenant_id: str | None = None
    # The entity the fact is about: provider id, tool id, runtime id, ...
    resource_id: str | None = None
    # Which component produced it, e.g. "translation", "runtime-manager".
    producer: str = "sutr"
    payload: dict[str, Any] = {}

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        tenant_id: uuid.UUID | str | None = None,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
        producer: str = "sutr",
        event_version: int = 1,
        correlation_id: str | None = None,
    ) -> "EventEnvelope":
        return cls(
            event_type=event_type,
            event_version=event_version,
            correlation_id=correlation_id or get_correlation_id(),
            tenant_id=str(tenant_id) if tenant_id else None,
            resource_id=resource_id,
            producer=producer,
            payload=payload or {},
        )

    def as_dict(self) -> dict:
        return self.model_dump(mode="json")
