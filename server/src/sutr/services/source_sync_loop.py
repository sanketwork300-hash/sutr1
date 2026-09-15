"""The background loop that checks watched sources.

Separate from `source_sync.py` so the sweep can be tested without a loop, and
so the loop can be moved to its own worker later without moving the logic.
"""

import asyncio
import logging

from sutr.config import settings
from sutr.services.source_sync import run_sync_sweep

logger = logging.getLogger(__name__)


async def source_sync_loop() -> None:
    """Check due sources on a fixed cadence.

    The cadence here is how often the platform *looks* for due sources, not how
    often any one source is polled — each source has its own interval, and its
    own failure backoff.
    """
    logger.info("source sync loop started")
    while True:
        await asyncio.sleep(settings.source_sync_interval_seconds)
        try:
            counts = await run_sync_sweep()
            if counts["checked"]:
                logger.info(
                    "source sync: checked %d, changed %d, failed %d",
                    counts["checked"],
                    counts["changed"],
                    counts["failed"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("source sync sweep failed")
