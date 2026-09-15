"""Retry with backoff, and the harder question of what deserves a retry.

Retrying is the easy half. The half that matters is deciding *which* failures
are worth retrying, because retrying the others is how a struggling provider is
turned into an unreachable one — and how a non-idempotent request is executed
twice.

Two rules decide it here:

- **Only transport failures are retried.** A connection that was refused, reset
  or timed out did not reach the provider's application, so repeating it cannot
  repeat a side effect. An HTTP response — any status — did reach it, and this
  platform does not know whether the tool it called was idempotent. A 500 from
  a provider is therefore *reported*, not retried.
- **Only safe methods are retried.** Even a transport failure can leave a
  request half-applied: a POST whose response was lost may have been executed.
  GET and HEAD are safe by definition; the rest are not, and are surfaced.

The result is that this retries fewer things than most retry helpers, on
purpose. The tool being called belongs to somebody else.
"""

import asyncio
import random

import httpx

from sutr.config import settings

# Methods that can be repeated without repeating an effect (RFC 9110 §9.2.1).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Failures that mean "the request did not arrive".
_TRANSPORT_FAILURES = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def is_retryable(error: BaseException, method: str) -> bool:
    """Whether repeating this exact request is safe and worth doing."""
    if method.upper() not in SAFE_METHODS:
        return False
    return isinstance(error, _TRANSPORT_FAILURES)


def is_provider_failure(error: BaseException) -> bool:
    """Whether this failure says something about the provider's health.

    A refused connection does. A malformed argument, an unsafe URL or a
    response that was too large do not — those are this platform's or the
    caller's problem, and counting them would open a circuit on a provider that
    is working perfectly.
    """
    return isinstance(error, (*_TRANSPORT_FAILURES, httpx.TimeoutException))


def backoff_seconds(attempt: int) -> float:
    """Exponential backoff with full jitter, bounded.

    Full jitter rather than a fixed multiple: without it, every caller that
    failed at the same moment retries at the same moment, and the provider that
    was recovering gets the whole herd at once.
    """
    ceiling = min(
        settings.provider_retry_base_seconds * (2**attempt),
        settings.provider_retry_max_seconds,
    )
    return random.uniform(0, ceiling)


async def with_retries(call, *, method: str, attempts: int | None = None):
    """Run `call()`, retrying transport failures on safe methods.

    Returns whatever `call` returns. Re-raises the last error when every
    attempt fails, so the caller sees the real failure rather than a wrapper.
    """
    limit = attempts if attempts is not None else settings.provider_retry_attempts
    last_error: BaseException | None = None

    for attempt in range(max(1, limit)):
        try:
            return await call()
        except Exception as error:  # noqa: PERF203 - the retry is the point
            last_error = error
            if attempt == limit - 1 or not is_retryable(error, method):
                raise
            await asyncio.sleep(backoff_seconds(attempt))

    raise last_error  # pragma: no cover - unreachable; the loop always returns or raises


def describe() -> dict:
    return {
        "attempts": settings.provider_retry_attempts,
        "base_seconds": settings.provider_retry_base_seconds,
        "max_seconds": settings.provider_retry_max_seconds,
        "jitter": "full",
        "retries": "transport failures on safe methods only",
    }
