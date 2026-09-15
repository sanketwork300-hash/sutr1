"""Stop hammering a provider that is failing (LLD §5.8).

The pattern is old and the reason is specific: when an upstream is down, every
call still costs a worker, a connection and the caller's patience, and the
answer is known in advance. A breaker turns "wait 30 seconds and fail" into
"fail now", which is both cheaper and more useful — an agent that gets a fast
`provider_unavailable` can try another tool, while one that gets a slow timeout
usually just retries.

Three states, and the middle one is the whole design:

    closed  ──(too many failures)──▶  open  ──(cool-off elapsed)──▶  half_open
      ▲                                                                  │
      └──────────────(a trial call succeeds)─────────────────────────────┘
                     (a trial call fails ⇒ open again)

**half_open lets exactly one call through.** Not a percentage, not a rate: one.
A provider that has just come back cannot be load-tested by the thing that is
supposed to be protecting it.

State is per process, and deliberately so. A shared breaker would need a store
on the hot path and would let one replica's view of a provider stop every other
replica's traffic. Per replica, N replicas make at most N trial calls — which
is the cost of not adding Redis to the critical path, and is written down in
`platform/scaling.py` with everything else that is per process.
"""

import threading
import time
from dataclasses import dataclass, field

from sutr.config import settings

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


@dataclass
class _State:
    """One provider's circuit, as seen by this process."""

    state: str = CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    # A trial is in flight: nobody else may make one.
    trial_in_flight: bool = False
    # Rolling counts, for the health score. Reset when the circuit closes, so
    # the score describes the current episode rather than all of history.
    successes: int = 0
    failures: int = 0
    last_error: str = ""
    last_change: float = field(default_factory=time.monotonic)


_lock = threading.Lock()
_circuits: dict[str, _State] = {}


class ProviderUnavailableError(Exception):
    """Raised instead of making a call the breaker already knows will fail."""

    def __init__(self, provider: str, retry_after: int, last_error: str = ""):
        self.provider = provider
        self.retry_after = retry_after
        self.last_error = last_error
        super().__init__(
            f"{provider} is not being called: it failed repeatedly and the circuit is open. "
            f"Retry in about {retry_after}s."
        )


def _circuit(provider: str) -> _State:
    circuit = _circuits.get(provider)
    if circuit is None:
        circuit = _State()
        _circuits[provider] = circuit
    return circuit


def allow(provider: str) -> None:
    """Raise `ProviderUnavailableError` if this call should not be made.

    Called immediately before dispatch, so the decision is as fresh as it can
    be, and cheap: one dictionary lookup under a lock held for microseconds.
    """
    if not settings.circuit_breaker_enabled:
        return

    now = time.monotonic()
    with _lock:
        circuit = _circuit(provider)
        if circuit.state == CLOSED:
            return

        if circuit.state == OPEN:
            elapsed = now - circuit.opened_at
            if elapsed < settings.circuit_breaker_cooloff_seconds:
                raise ProviderUnavailableError(
                    provider,
                    retry_after=max(1, int(settings.circuit_breaker_cooloff_seconds - elapsed)),
                    last_error=circuit.last_error,
                )
            circuit.state = HALF_OPEN
            circuit.trial_in_flight = False
            circuit.last_change = now

        # half_open: one trial call, and one only.
        if circuit.trial_in_flight:
            raise ProviderUnavailableError(
                provider,
                retry_after=max(1, int(settings.circuit_breaker_cooloff_seconds)),
                last_error=circuit.last_error,
            )
        circuit.trial_in_flight = True


def record_success(provider: str) -> None:
    """A call came back. Closing on the first success is deliberate: the trial
    call in half_open exists precisely to answer "is it back?"."""
    with _lock:
        circuit = _circuit(provider)
        circuit.successes += 1
        circuit.consecutive_failures = 0
        circuit.trial_in_flight = False
        if circuit.state != CLOSED:
            circuit.state = CLOSED
            circuit.opened_at = 0.0
            circuit.last_change = time.monotonic()
            circuit.successes = 1
            circuit.failures = 0
            circuit.last_error = ""


def record_failure(provider: str, error: str = "") -> None:
    """A call failed in a way that says something about the provider.

    Not every error qualifies — a 404 from a provider is a working provider —
    and the caller decides. See `retry.is_provider_failure`.
    """
    with _lock:
        circuit = _circuit(provider)
        circuit.failures += 1
        circuit.consecutive_failures += 1
        circuit.last_error = error[:200]
        circuit.trial_in_flight = False

        if circuit.state == HALF_OPEN:
            # The trial failed: back to open, and the cool-off starts again.
            circuit.state = OPEN
            circuit.opened_at = time.monotonic()
            circuit.last_change = circuit.opened_at
            return

        if (
            circuit.state == CLOSED
            and circuit.consecutive_failures >= settings.circuit_breaker_failure_threshold
        ):
            circuit.state = OPEN
            circuit.opened_at = time.monotonic()
            circuit.last_change = circuit.opened_at


def health(provider: str) -> dict:
    """A provider's health as this process sees it.

    The score is 0–100 and is a *description*, not an input to anything: it is
    what an operator reads. Decisions are made by the state machine above,
    which is discrete and explainable, rather than by a threshold on a number
    nobody can reconstruct.
    """
    with _lock:
        circuit = _circuits.get(provider)
        if circuit is None:
            return {
                "provider": provider,
                "state": CLOSED,
                "score": None,  # nothing observed yet is not the same as healthy
                "successes": 0,
                "failures": 0,
            }
        total = circuit.successes + circuit.failures
        score = round(100 * circuit.successes / total) if total else None
        return {
            "provider": provider,
            "state": circuit.state,
            "score": score,
            "successes": circuit.successes,
            "failures": circuit.failures,
            "consecutive_failures": circuit.consecutive_failures,
            "last_error": circuit.last_error or None,
            "quarantined": circuit.state != CLOSED,
            "seconds_in_state": round(time.monotonic() - circuit.last_change, 1),
        }


def snapshot() -> list[dict]:
    """Every provider this process has an opinion about."""
    with _lock:
        providers = sorted(_circuits)
    return [health(provider) for provider in providers]


def reset(provider: str | None = None) -> None:
    """Forget everything, or one provider. For tests and for an operator who
    has fixed the upstream and does not want to wait out the cool-off."""
    with _lock:
        if provider is None:
            _circuits.clear()
        else:
            _circuits.pop(provider, None)


def describe() -> dict:
    return {
        "enabled": settings.circuit_breaker_enabled,
        "failure_threshold": settings.circuit_breaker_failure_threshold,
        "cooloff_seconds": settings.circuit_breaker_cooloff_seconds,
        "scope": "per process",
        "half_open_trial_calls": 1,
    }
