"""Connect, read, and overall — on every remote call (LLD §5.8).

One number is not a timeout policy. "30 seconds" spent waiting for a TCP
handshake to a host that is gone is a different failure from 30 seconds spent
reading a slow report, and they want different limits: a connect that has not
completed in a few seconds is not going to, while a read that takes twenty is
often a provider doing real work.

Separating them also makes the *fast* failure fast. Before this, a provider
whose host had disappeared occupied a worker for the full call budget; now it
fails in `connect_seconds` and the circuit breaker sees the failure sooner.
"""

import httpx

from sutr.config import settings


def provider_timeout(overall_seconds: float | None = None) -> httpx.Timeout:
    """The timeout for one outbound provider call.

    `overall_seconds` overrides the configured ceiling for a caller that knows
    better — a deployment probe, say. Everything else takes the configured
    policy, which is the point of having one.
    """
    total = overall_seconds if overall_seconds is not None else settings.provider_timeout_seconds
    return httpx.Timeout(
        # The overall budget, which every phase is also bounded by.
        total,
        connect=min(settings.provider_connect_timeout_seconds, total),
        read=min(settings.provider_read_timeout_seconds, total),
        write=min(settings.provider_read_timeout_seconds, total),
        # Waiting for a free connection from the pool is a *local* queue. A long
        # wait here means this process is saturated, not that the provider is
        # slow, and failing quickly is what keeps that distinction visible.
        pool=min(settings.provider_pool_timeout_seconds, total),
    )


def describe() -> dict:
    return {
        "overall_seconds": settings.provider_timeout_seconds,
        "connect_seconds": settings.provider_connect_timeout_seconds,
        "read_seconds": settings.provider_read_timeout_seconds,
        "pool_seconds": settings.provider_pool_timeout_seconds,
    }
