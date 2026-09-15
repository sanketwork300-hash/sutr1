"""Standalone event relay.

The relay runs inside the API process by default, which is right for a single
replica and wrong for several: every replica would poll the same outbox rows
and contend. With more than one API replica, set `EVENT_RELAY_ENABLED=false`
and run exactly one of these instead:

    uv run sutr-event-relay

It is the same `relay_loop` the API would have run, so there is one
implementation and no chance of the two drifting.
"""

import asyncio
import logging
import sys

from sutr.config import settings
from sutr.events.relay import relay_loop
from sutr.observability.log_setup import configure_logging


def main() -> None:
    configure_logging(
        level=settings.log_level,
        log_format=settings.log_format,
        service=f"{settings.otel_service_name}-relay",
    )
    logger = logging.getLogger(__name__)
    if settings.event_relay_enabled:
        logger.warning(
            "EVENT_RELAY_ENABLED is true, so the API process is also relaying. Two relays "
            "will contend for the same outbox rows — set it to false when running this worker."
        )
    try:
        asyncio.run(relay_loop())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
