"""Retry, backoff and timeout policy (LLD §5.8).

The interesting assertions here are the negative ones. This retries fewer
things than a typical retry helper — no POSTs, no HTTP error responses — and
each refusal is a decision about somebody else's system: a request that reached
a provider may have been executed, and this platform does not know whether the
tool it called was idempotent.
"""

import asyncio

import httpx
import pytest

from sutr.config import settings
from sutr.resilience import retry
from sutr.resilience.timeouts import provider_timeout

# ── What may be retried ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("no route"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("saturated"),
        httpx.RemoteProtocolError("truncated"),
    ],
)
def test_transport_failures_on_a_get_are_retryable(error):
    assert retry.is_retryable(error, "GET") is True


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_nothing_is_retried_on_an_unsafe_method(method):
    """A POST whose response was lost may have been executed. Repeating it is a
    decision about somebody else's system that this platform cannot make."""
    assert retry.is_retryable(httpx.ConnectError("refused"), method) is False


def test_an_http_error_response_is_not_retried():
    """A response of any status reached the provider's application. Whether the
    call is safe to repeat is a property of the tool, which is not knowable
    from here."""
    error = httpx.HTTPStatusError(
        "500", request=httpx.Request("GET", "https://x.example"), response=httpx.Response(500)
    )
    assert retry.is_retryable(error, "GET") is False


def test_a_programming_error_is_not_retried():
    assert retry.is_retryable(ValueError("bad argument"), "GET") is False


# ── Backoff ──────────────────────────────────────────────────────────────────


def test_backoff_grows_and_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "provider_retry_base_seconds", 0.2)
    monkeypatch.setattr(settings, "provider_retry_max_seconds", 1.0)

    ceilings = [max(retry.backoff_seconds(attempt) for _ in range(50)) for attempt in range(5)]
    assert ceilings[0] <= 0.2
    assert ceilings[-1] <= 1.0
    assert ceilings[2] > ceilings[0]


def test_backoff_is_jittered_so_callers_do_not_return_together():
    """Without jitter, everything that failed at the same moment retries at the
    same moment, and the provider that was recovering gets the whole herd."""
    samples = {round(retry.backoff_seconds(3), 6) for _ in range(50)}
    assert len(samples) > 10


# ── The retry loop ───────────────────────────────────────────────────────────


async def test_a_transient_failure_is_retried_and_then_succeeds(monkeypatch):
    monkeypatch.setattr(settings, "provider_retry_base_seconds", 0.0)
    attempts = {"n": 0}

    async def call():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused")
        return "ok"

    assert await retry.with_retries(call, method="GET", attempts=3) == "ok"
    assert attempts["n"] == 2


async def test_the_last_error_is_raised_rather_than_a_wrapper(monkeypatch):
    monkeypatch.setattr(settings, "provider_retry_base_seconds", 0.0)

    async def call():
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        await retry.with_retries(call, method="GET", attempts=3)


async def test_an_unsafe_method_is_attempted_exactly_once(monkeypatch):
    monkeypatch.setattr(settings, "provider_retry_base_seconds", 0.0)
    attempts = {"n": 0}

    async def call():
        attempts["n"] += 1
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        await retry.with_retries(call, method="POST", attempts=5)
    assert attempts["n"] == 1


async def test_a_successful_call_is_not_retried():
    attempts = {"n": 0}

    async def call():
        attempts["n"] += 1
        return "ok"

    await retry.with_retries(call, method="GET", attempts=3)
    assert attempts["n"] == 1


async def test_retries_actually_wait(monkeypatch):
    """Backoff that does not sleep is not backoff."""
    monkeypatch.setattr(settings, "provider_retry_base_seconds", 10.0)
    monkeypatch.setattr(settings, "provider_retry_max_seconds", 10.0)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def call():
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        await retry.with_retries(call, method="GET", attempts=3)
    assert len(slept) == 2
    assert all(delay >= 0 for delay in slept)


# ── Timeouts ─────────────────────────────────────────────────────────────────


def test_connect_read_and_pool_are_separate(monkeypatch):
    """One number is not a timeout policy: waiting for a handshake to a host
    that is gone is a different failure from reading a slow report."""
    monkeypatch.setattr(settings, "provider_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "provider_connect_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "provider_read_timeout_seconds", 25.0)
    monkeypatch.setattr(settings, "provider_pool_timeout_seconds", 2.0)

    policy = provider_timeout()

    assert policy.connect == 5.0
    assert policy.read == 25.0
    assert policy.pool == 2.0


def test_no_phase_may_outlast_the_overall_budget(monkeypatch):
    """A caller asking for five seconds overall must not wait thirty to connect."""
    monkeypatch.setattr(settings, "provider_connect_timeout_seconds", 30.0)
    monkeypatch.setattr(settings, "provider_read_timeout_seconds", 30.0)

    policy = provider_timeout(5.0)

    assert policy.connect == 5.0
    assert policy.read == 5.0


def test_a_caller_that_passes_a_number_still_gets_the_full_policy():
    from sutr.api_client import _timeout_policy

    policy = _timeout_policy(12.0)
    assert isinstance(policy, httpx.Timeout)
    assert policy.connect <= 12.0


def test_an_explicit_policy_is_honoured_unchanged():
    from sutr.api_client import _timeout_policy

    explicit = httpx.Timeout(9.0, connect=1.0)
    assert _timeout_policy(explicit) is explicit
