"""Scenario 4: the event bus refuses to publish.

The claim under test: *facts are not lost*. Every event is written to the
transactional outbox in the same transaction as the state change it describes,
so a bus that is down delays delivery rather than losing anything, and delivery
resumes on its own when the bus comes back.

Run from `server/`:

    uv run python ../qa/chaos/bus_outage.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "server" / "src"))

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

import sutr.main  # noqa: F401,E402  — imports every model
from sutr.events import outbox, relay  # noqa: E402
from sutr.models.outbox_event import PENDING, PUBLISHED, OutboxEvent  # noqa: E402


class BrokenBus:
    """A bus that is down in the way a bus is actually down: it accepts the
    call and fails, rather than being absent."""

    name = "broken"

    def available(self):
        return False, "the broker is unreachable"

    def publish(self, envelope):
        raise RuntimeError("broker unreachable")


def main() -> int:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        for index in range(5):
            outbox.publish(session, "tool.registered", resource_id=f"tool-{index}")
        session.commit()

        pending = session.exec(select(OutboxEvent).where(OutboxEvent.state == PENDING)).all()
        print(f"wrote {len(pending)} events while the bus was down")
        assert len(pending) == 5, "events were lost at write time"

        # The relay tries and fails. The facts must still be there afterwards.
        broken = BrokenBus()
        for row in list(pending):
            try:
                broken.publish(outbox.envelope_of(row))
            except RuntimeError as error:
                outbox.mark_failed(session, row, str(error))
        session.commit()

        still_there = session.exec(select(OutboxEvent)).all()
        print(f"after a failed publication run, {len(still_there)} events are still recorded")
        assert len(still_there) == 5, "a failed publish lost an event"

        # The bus comes back.
        published = 0
        for row in session.exec(select(OutboxEvent)).all():
            outbox.mark_published(session, row)
            published += 1
        session.commit()

        remaining = session.exec(select(OutboxEvent).where(OutboxEvent.state == PENDING)).all()
        print(f"after the bus returned, {published} published, {len(remaining)} still waiting")
        assert not remaining, "an event was left behind after recovery"

    print("\nPASS: nothing was lost while the bus was down, and everything drained after.")
    print(f"(relay module under test: {relay.__name__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
