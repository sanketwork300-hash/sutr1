"""The tool lifecycle state machine.

The ESDS LLD calls this *"the shared vocabulary for the whole platform"* (§3.1,
p. 11) and lists fourteen states in order:

    DRAFT → API_UPLOADED → TRANSLATING → IR_READY → DOC_PROCESSING →
    METADATA_READY → GENERATING_MCP → VALIDATING → DEPLOYING → DEPLOYED →
    UNDER_REVIEW → APPROVED → PUBLISHED → ACTIVE

§3.7 draws a five-state summary of the same machine (DRAFT → VALIDATED →
DEPLOYED → UNDER_REVIEW → PUBLISHED). It is a summary, not a second machine, so
there is one vocabulary in this codebase rather than two that would drift.

Three rules shape the table below, and each is a decision rather than an
accident:

**Failure states branch off and resume.** §4.1: *"failure states … resume from
last good stage"*. `TRANSLATION_FAILED` goes back to `TRANSLATING`, not to
`DRAFT` — sending a provider back to the beginning because a spec failed to
parse would discard every stage that succeeded.

**Forward is not the only direction.** A published tool can be suspended, and a
suspended one restored. A deployed tool can be regenerated after a fix. The
pipeline is linear; the lifecycle is not.

**Two transitions are gated.** Entering `UNDER_REVIEW` and moving to
`PUBLISHED` need governance to allow them (§3.7). Everything else the platform
does on its own, because a gate on `TRANSLATING → IR_READY` would be a human
approving a parser.
"""

from dataclasses import dataclass
from typing import Any

from sutr.models.registry_tool import (
    ACTIVE,
    API_UPLOADED,
    APPROVED,
    ARCHIVED,
    DEPLOYED,
    DEPLOYING,
    DEPLOYMENT_FAILED,
    DEPRECATED,
    DOC_PROCESSING,
    DOCUMENTATION_FAILED,
    DRAFT,
    GENERATING_MCP,
    GENERATION_FAILED,
    IR_READY,
    METADATA_READY,
    PUBLISHED,
    REJECTED,
    SUSPENDED,
    TRANSLATING,
    TRANSLATION_FAILED,
    UNDER_REVIEW,
    VALIDATING,
)

# In the LLD's order. Used for display and for "how far along is this".
PIPELINE: tuple[str, ...] = (
    DRAFT,
    API_UPLOADED,
    TRANSLATING,
    IR_READY,
    DOC_PROCESSING,
    METADATA_READY,
    GENERATING_MCP,
    VALIDATING,
    DEPLOYING,
    DEPLOYED,
    UNDER_REVIEW,
    APPROVED,
    PUBLISHED,
    ACTIVE,
)

FAILURE_STATES: tuple[str, ...] = (
    TRANSLATION_FAILED,
    DOCUMENTATION_FAILED,
    GENERATION_FAILED,
    DEPLOYMENT_FAILED,
    REJECTED,
    SUSPENDED,
)

RETIRED_STATES: tuple[str, ...] = (DEPRECATED, ARCHIVED)

ALL_STATES: tuple[str, ...] = PIPELINE + FAILURE_STATES + RETIRED_STATES

# Where a failure resumes from. This is the "resumable after fixes" clause,
# written down rather than left to whoever handles the retry.
RESUME_FROM: dict[str, str] = {
    TRANSLATION_FAILED: TRANSLATING,
    DOCUMENTATION_FAILED: DOC_PROCESSING,
    GENERATION_FAILED: GENERATING_MCP,
    DEPLOYMENT_FAILED: DEPLOYING,
    REJECTED: UNDER_REVIEW,
    SUSPENDED: PUBLISHED,
}

# Transitions governance must allow before they take effect (§3.7).
GATED: frozenset[tuple[str, str]] = frozenset(
    {
        (DEPLOYED, UNDER_REVIEW),
        (APPROVED, PUBLISHED),
    }
)

_FORWARD: dict[str, set[str]] = {
    DRAFT: {API_UPLOADED},
    API_UPLOADED: {TRANSLATING},
    TRANSLATING: {IR_READY, TRANSLATION_FAILED},
    IR_READY: {DOC_PROCESSING, METADATA_READY},
    DOC_PROCESSING: {METADATA_READY, DOCUMENTATION_FAILED},
    METADATA_READY: {GENERATING_MCP},
    GENERATING_MCP: {VALIDATING, GENERATION_FAILED},
    VALIDATING: {DEPLOYING, GENERATION_FAILED},
    DEPLOYING: {DEPLOYED, DEPLOYMENT_FAILED},
    DEPLOYED: {UNDER_REVIEW, GENERATING_MCP},
    UNDER_REVIEW: {APPROVED, REJECTED},
    APPROVED: {PUBLISHED},
    PUBLISHED: {ACTIVE, SUSPENDED, DEPRECATED},
    ACTIVE: {SUSPENDED, DEPRECATED, GENERATING_MCP},
    DEPRECATED: {ARCHIVED, PUBLISHED},
    ARCHIVED: set(),
}

# Failure states resume, and can also be abandoned back to DRAFT — a provider
# who has decided the spec was the wrong one entirely should not have to
# invent a fix to get out.
for _failure, _resume in RESUME_FROM.items():
    _FORWARD[_failure] = {_resume, DRAFT}
# SUSPENDED is the exception: a suspended tool has already been published and
# has subscribers, so it is restored, deprecated or archived — never reverted to
# DRAFT, which would orphan them.
_FORWARD[SUSPENDED] = {PUBLISHED, DEPRECATED, ARCHIVED}
_FORWARD[REJECTED] = {UNDER_REVIEW, DRAFT, GENERATING_MCP}

TRANSITIONS: dict[str, frozenset[str]] = {
    state: frozenset(targets) for state, targets in _FORWARD.items()
}


class TransitionError(ValueError):
    """A transition the state machine does not allow. The message names both ends."""


@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    gated: bool

    def as_dict(self) -> dict[str, Any]:
        return {"from": self.source, "to": self.target, "requires_approval": self.gated}


def is_known(state: str) -> bool:
    return state in ALL_STATES


def allowed_targets(state: str) -> tuple[str, ...]:
    return tuple(sorted(TRANSITIONS.get(state, frozenset())))


def requires_approval(source: str, target: str) -> bool:
    return (source, target) in GATED


def check(source: str, target: str) -> Transition:
    """Validate a transition, or raise with both ends named.

    Raising rather than returning a boolean because every caller of this would
    otherwise have to construct the same message, and half of them would
    construct a worse one.
    """
    if not is_known(target):
        raise TransitionError(f"'{target}' is not a lifecycle state.")
    if source == target:
        raise TransitionError(f"The tool is already {source}.")
    if target not in TRANSITIONS.get(source, frozenset()):
        options = ", ".join(allowed_targets(source)) or "nothing — this is a terminal state"
        raise TransitionError(
            f"A tool in {source} cannot move to {target}. It can move to: {options}."
        )
    return Transition(source=source, target=target, gated=requires_approval(source, target))


def progress(state: str) -> dict[str, Any]:
    """How far along the pipeline a tool is, for display.

    A failure state reports the progress of the stage it will resume at rather
    than falling off the scale: a tool that failed deployment is nine steps in
    and stuck, not zero steps in.
    """
    reference = RESUME_FROM.get(state, state)
    if reference in PIPELINE:
        return {
            "step": PIPELINE.index(reference) + 1,
            "of": len(PIPELINE),
            "stage": reference,
            "failed": state in FAILURE_STATES,
            "retired": state in RETIRED_STATES,
        }
    return {
        "step": None,
        "of": len(PIPELINE),
        "stage": state,
        "failed": state in FAILURE_STATES,
        "retired": state in RETIRED_STATES,
    }


def describe() -> dict[str, Any]:
    """The whole machine, for a client that would rather not hard-code it."""
    return {
        "pipeline": list(PIPELINE),
        "failure_states": list(FAILURE_STATES),
        "retired_states": list(RETIRED_STATES),
        "resume_from": dict(RESUME_FROM),
        "gated_transitions": [{"from": source, "to": target} for source, target in sorted(GATED)],
        "transitions": {state: list(allowed_targets(state)) for state in ALL_STATES},
    }
