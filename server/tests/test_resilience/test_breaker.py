"""The circuit breaker, and what it deliberately does not count (LLD §5.8).

Two properties matter more than the state machine:

1. **half_open lets exactly one call through.** A provider that has just come
   back must not be load-tested by the thing protecting it.
2. **Only failures that say something about the provider count.** A 404 is a
   working provider disagreeing with a request; opening a circuit on a run of
   them takes a healthy provider out of service for everybody.
"""

import time

import httpx
import pytest

from sutr.config import settings
from sutr.resilience import breaker

PROVIDER = "api.provider.example"


@pytest.fixture(autouse=True)
def clean_circuits(monkeypatch):
    breaker.reset()
    monkeypatch.setattr(settings, "circuit_breaker_enabled", True)
    monkeypatch.setattr(settings, "circuit_breaker_failure_threshold", 3)
    monkeypatch.setattr(settings, "circuit_breaker_cooloff_seconds", 30.0)
    yield
    breaker.reset()


def _fail(times: int) -> None:
    for _ in range(times):
        breaker.allow(PROVIDER)
        breaker.record_failure(PROVIDER, "connection refused")


# ── Opening ──────────────────────────────────────────────────────────────────


def test_a_healthy_provider_is_called(session=None):
    breaker.allow(PROVIDER)  # does not raise
    assert breaker.health(PROVIDER)["state"] == breaker.CLOSED


def test_failures_below_the_threshold_do_not_open_it():
    _fail(2)
    breaker.allow(PROVIDER)
    assert breaker.health(PROVIDER)["state"] == breaker.CLOSED


def test_the_threshold_opens_it_and_the_next_call_fails_immediately():
    _fail(3)

    with pytest.raises(breaker.ProviderUnavailableError) as raised:
        breaker.allow(PROVIDER)

    assert raised.value.provider == PROVIDER
    assert raised.value.retry_after > 0
    # The refusal says what happened last, so the caller is not left guessing.
    assert "connection refused" in raised.value.last_error


def test_a_success_resets_the_run_of_failures():
    _fail(2)
    breaker.allow(PROVIDER)
    breaker.record_success(PROVIDER)
    _fail(2)

    breaker.allow(PROVIDER)  # five failures, but never three in a row
    assert breaker.health(PROVIDER)["state"] == breaker.CLOSED


# ── The cool-off, and the single trial call ──────────────────────────────────


def test_the_circuit_stays_open_for_the_cool_off(monkeypatch):
    _fail(3)
    with pytest.raises(breaker.ProviderUnavailableError):
        breaker.allow(PROVIDER)


def test_after_the_cool_off_exactly_one_call_is_allowed_through(monkeypatch):
    """Not a percentage, not a rate: one. A provider that has just come back
    cannot be load-tested by the thing protecting it."""
    _fail(3)
    monkeypatch.setattr(settings, "circuit_breaker_cooloff_seconds", 0.0)

    breaker.allow(PROVIDER)  # the trial
    assert breaker.health(PROVIDER)["state"] == breaker.HALF_OPEN

    with pytest.raises(breaker.ProviderUnavailableError):
        breaker.allow(PROVIDER)  # everybody else waits


def test_a_successful_trial_closes_the_circuit(monkeypatch):
    _fail(3)
    monkeypatch.setattr(settings, "circuit_breaker_cooloff_seconds", 0.0)

    breaker.allow(PROVIDER)
    breaker.record_success(PROVIDER)

    assert breaker.health(PROVIDER)["state"] == breaker.CLOSED
    breaker.allow(PROVIDER)  # traffic resumes


def test_a_failed_trial_opens_it_again_and_restarts_the_cool_off(monkeypatch):
    _fail(3)
    monkeypatch.setattr(settings, "circuit_breaker_cooloff_seconds", 0.0)
    breaker.allow(PROVIDER)
    breaker.record_failure(PROVIDER, "still down")

    monkeypatch.setattr(settings, "circuit_breaker_cooloff_seconds", 30.0)
    with pytest.raises(breaker.ProviderUnavailableError):
        breaker.allow(PROVIDER)
    assert breaker.health(PROVIDER)["state"] == breaker.OPEN


def test_one_provider_failing_does_not_affect_another():
    _fail(3)
    breaker.allow("other.provider.example")  # untouched


# ── The health score ─────────────────────────────────────────────────────────


def test_a_provider_never_called_has_no_score_rather_than_a_perfect_one():
    """Nothing observed is not the same as healthy."""
    assert breaker.health("never.called.example")["score"] is None


def test_the_score_describes_the_current_episode():
    for _ in range(9):
        breaker.record_success(PROVIDER)
    breaker.record_failure(PROVIDER, "blip")

    assert breaker.health(PROVIDER)["score"] == 90


def test_closing_the_circuit_starts_the_score_again(monkeypatch):
    _fail(3)
    monkeypatch.setattr(settings, "circuit_breaker_cooloff_seconds", 0.0)
    breaker.allow(PROVIDER)
    breaker.record_success(PROVIDER)

    # The score describes how the provider is doing now, not how it did during
    # the outage it has recovered from.
    assert breaker.health(PROVIDER)["score"] == 100


def test_a_quarantined_provider_says_so():
    _fail(3)
    health = breaker.health(PROVIDER)
    assert health["quarantined"] is True
    assert health["consecutive_failures"] == 3
    assert health["seconds_in_state"] >= 0


def test_the_snapshot_lists_every_provider_seen():
    breaker.record_success("a.example")
    breaker.record_failure("b.example", "x")

    assert [entry["provider"] for entry in breaker.snapshot()] == ["a.example", "b.example"]


# ── Disabled, and reset ──────────────────────────────────────────────────────


def test_a_disabled_breaker_never_refuses(monkeypatch):
    monkeypatch.setattr(settings, "circuit_breaker_enabled", False)
    for _ in range(50):
        breaker.record_failure(PROVIDER, "down")

    breaker.allow(PROVIDER)  # does not raise


def test_an_operator_can_close_a_circuit_by_hand():
    _fail(3)
    breaker.reset(PROVIDER)
    breaker.allow(PROVIDER)


# ── What counts as a provider failure ────────────────────────────────────────


def test_transport_failures_count_and_argument_errors_do_not():
    from sutr.resilience import retry

    assert retry.is_provider_failure(httpx.ConnectError("refused")) is True
    assert retry.is_provider_failure(httpx.ReadTimeout("slow")) is True
    # This platform's own problem, or the caller's — not the provider's.
    assert retry.is_provider_failure(ValueError("bad argument")) is False
    assert retry.is_provider_failure(KeyError("missing")) is False


def test_the_policy_report_says_what_is_configured():
    described = breaker.describe()
    assert described["enabled"] is True
    assert described["half_open_trial_calls"] == 1
    assert described["scope"] == "per process"


def test_state_is_process_local_and_declared_as_such():
    """The scaling inventory is where a reader learns this. If the breaker
    stops being per-process, that entry has to change with it."""
    from sutr.platform import scaling

    entry = next(
        item
        for item in scaling.describe()["process_local_state"]
        if item["name"] == "circuit breaker state"
    )
    assert entry["location"] == "sutr.resilience.breaker._circuits"


def test_the_refusal_is_fast():
    """The whole point: a known-bad provider costs a dictionary lookup, not a
    connection attempt and a timeout."""
    _fail(3)
    started = time.perf_counter()
    for _ in range(1000):
        try:
            breaker.allow(PROVIDER)
        except breaker.ProviderUnavailableError:
            pass
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"1000 refusals took {elapsed:.3f}s"


def test_a_failure_with_no_message_still_records_a_reason():
    """Found live: `str(httpx.ConnectTimeout(...))` is often empty, so an
    operator saw `last_error: null` after five failures — the circuit was open
    and the report said nothing about why."""
    import httpx

    from sutr.resilience import retry

    error = httpx.ConnectTimeout("")
    assert retry.is_provider_failure(error) is True
    breaker.record_failure(PROVIDER, str(error) or error.__class__.__name__)

    assert breaker.health(PROVIDER)["last_error"] == "ConnectTimeout"
