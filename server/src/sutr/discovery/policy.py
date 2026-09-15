"""The policy filter, which runs *before* ranking.

LLD §3.8 is explicit about both the checks and the order:

    Policy filter checks tenant, subscription, region, compliance, provider
    policy, runtime status, visibility, lifecycle state — **before** ranking.

Order first, because it is the part that is easy to get backwards. Ranking is
the expensive half of a discovery request; ranking a tool the caller may not use
and then dropping it spends that half on an answer that was never available.
Filtering first also means the ranker never sees a candidate whose score could
leak information about a tool the caller cannot see.

Eight checks, each returning a **reason** when it excludes something. The
reasons are the point. LLD §4.2's hot-path table says a request with no match
should return *"suggestions or 'tool not found'"* — an agent handed an empty
list learns nothing, while an agent told *"three tools matched your intent but
you are not subscribed to them"* can do something about it. So exclusions are
kept, not discarded, and the service returns them as suggestions when nothing
survives.

The filter is **versioned**. `POLICY_VERSION` is part of the cache key, so
changing a rule invalidates every cached answer that was computed under the old
one rather than serving decisions the platform no longer makes.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sutr.discovery.corpus import Candidate
from sutr.models.registry_tool import (
    ACTIVE,
    ARCHIVED,
    DEPRECATED,
    PUBLISHED,
    VISIBILITY_ORGANIZATION,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
)
from sutr.models.tool_subscription import STATE_ACTIVE

# Bumped when a rule changes in a way that would make a cached decision wrong.
# Not bumped for a reworded reason.
#
# History:
#   1  tenant, subscription, region, compliance, provider policy, runtime
#      status, visibility, lifecycle
POLICY_VERSION = 1

# Lifecycle states a discoverable tool may be in. A DEPRECATED tool is still
# discoverable *to the tenants already using it* — see below — but is not
# offered to anybody else.
DISCOVERABLE_STATES = (PUBLISHED, ACTIVE)

CHECKS = (
    "tenant",
    "subscription",
    "region",
    "compliance",
    "provider_policy",
    "runtime_status",
    "visibility",
    "lifecycle",
)


@dataclass
class Requirements:
    """What the caller needs a tool to satisfy.

    Everything is optional. A caller who does not state a region is not
    filtered by region — inventing a default region would silently hide tools
    for a reason the caller never asked for.
    """

    region: str | None = None
    compliance: list[str] = field(default_factory=list)
    # Only tools the caller can invoke right now. Off by default: discovery is
    # how you find something to subscribe *to*.
    entitled_only: bool = False
    # Include tools that are deprecated, for a caller migrating off one.
    include_deprecated: bool = False
    categories: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "compliance": self.compliance,
            "entitled_only": self.entitled_only,
            "include_deprecated": self.include_deprecated,
            "categories": self.categories,
        }


@dataclass
class Exclusion:
    candidate: Candidate
    check: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": str(self.candidate.tool_id),
            "tool_key": self.candidate.tool_key,
            "name": self.candidate.name,
            "check": self.check,
            "reason": self.reason,
        }


@dataclass
class FilterResult:
    allowed: list[Candidate]
    excluded: list[Exclusion]
    version: int = POLICY_VERSION

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for exclusion in self.excluded:
            counts[exclusion.check] = counts.get(exclusion.check, 0) + 1
        return {
            "version": self.version,
            "checks": list(CHECKS),
            "allowed": len(self.allowed),
            "excluded": len(self.excluded),
            "excluded_by": counts,
        }


def _check(
    candidate: Candidate, org_id: uuid.UUID, requirements: Requirements
) -> tuple[str, str] | None:
    """The first check this candidate fails, or None. Order is the LLD's."""

    # 1. Tenant. A private tool belongs to its provider and to nobody else.
    if candidate.visibility == VISIBILITY_PRIVATE and candidate.provider_org_id != org_id:
        return "tenant", "This tool is private to its provider."

    # 2. Visibility. Organization-visible tools are visible inside the
    #    provider's organization only.
    if candidate.visibility == VISIBILITY_ORGANIZATION and candidate.provider_org_id != org_id:
        return "visibility", "This tool is visible only inside its provider's organization."
    if candidate.visibility not in (VISIBILITY_PRIVATE, VISIBILITY_ORGANIZATION, VISIBILITY_PUBLIC):
        return "visibility", f"Unknown visibility '{candidate.visibility}'."

    # 3. Lifecycle. Archived is excluded for everyone, its own provider
    #    included: a retired tool is not a recommendation, and the provider who
    #    retired it does not want it offered back to them either. Below that, a
    #    provider sees their own work in progress and nobody else does.
    if candidate.lifecycle_state == ARCHIVED:
        return "lifecycle", "This tool has been archived."
    if candidate.owned:
        pass
    elif candidate.lifecycle_state == DEPRECATED:
        if not (requirements.include_deprecated or candidate.subscription_state == STATE_ACTIVE):
            return "lifecycle", (
                "This tool is deprecated and is not offered to new consumers. Pass "
                "include_deprecated to see it."
            )
    elif candidate.lifecycle_state not in DISCOVERABLE_STATES:
        return "lifecycle", (
            f"This tool is {candidate.lifecycle_state}, not published, so it is not offered yet."
        )

    # 4. Subscription. Only when the caller asked for tools they can use now;
    #    otherwise an unsubscribed tool is a *result*, not an exclusion.
    if requirements.entitled_only and not candidate.owned:
        if candidate.subscription_state != STATE_ACTIVE:
            return "subscription", (
                "You are not subscribed to this tool."
                if candidate.subscription_state is None
                else f"Your subscription is {candidate.subscription_state}, not active."
            )

    # 5. Region. A declared region list that does not include the caller's is a
    #    refusal by the provider; an empty list is no claim either way.
    if requirements.region and candidate.regions and requirements.region not in candidate.regions:
        return "region", (
            f"The provider declares this tool for {', '.join(candidate.regions)}, "
            f"not {requirements.region}."
        )

    # 6. Compliance. Every regime the caller asked for must be declared.
    missing = [
        regime
        for regime in requirements.compliance
        if regime.lower() not in {claim.lower() for claim in candidate.compliance}
    ]
    if missing:
        return "compliance", (f"The provider does not declare {', '.join(missing)} for this tool.")

    # 7. Provider policy. The provider's own restriction on who may use it.
    #    Sutr has no per-tool provider policy yet, so this check is declared and
    #    passes — named rather than silently missing, so its absence is visible
    #    in `describe()` instead of looking like a check that ran.
    if candidate.extra.get("provider_policy_denies"):
        return "provider_policy", str(candidate.extra["provider_policy_denies"])

    # 8. Runtime status. A tool whose runtime cannot serve it is not a useful
    #    recommendation.
    if candidate.extra.get("runtime_unavailable"):
        return "runtime_status", str(candidate.extra["runtime_unavailable"])

    # Category is not one of the LLD's eight; it is a caller-supplied narrowing
    # and is applied last so its exclusions are distinguishable from policy.
    if requirements.categories and candidate.category not in requirements.categories:
        return "category", f"Not in {', '.join(requirements.categories)}."

    return None


def apply(
    candidates: list[Candidate], *, org_id: uuid.UUID, requirements: Requirements
) -> FilterResult:
    allowed: list[Candidate] = []
    excluded: list[Exclusion] = []
    for candidate in candidates:
        failure = _check(candidate, org_id, requirements)
        if failure is None:
            allowed.append(candidate)
        else:
            check, reason = failure
            excluded.append(Exclusion(candidate=candidate, check=check, reason=reason))
    return FilterResult(allowed=allowed, excluded=excluded)


def describe() -> dict[str, Any]:
    """Which checks run, and which are declared but have nothing to read."""
    return {
        "version": POLICY_VERSION,
        "checks": [
            {"name": "tenant", "enforced": True},
            {"name": "visibility", "enforced": True},
            {"name": "lifecycle", "enforced": True},
            {"name": "subscription", "enforced": True, "detail": "only when entitled_only is set"},
            {"name": "region", "enforced": True, "detail": "against the provider's declaration"},
            {
                "name": "compliance",
                "enforced": True,
                "detail": "against the provider's declaration; nothing verifies it",
            },
            {
                "name": "provider_policy",
                "enforced": False,
                "detail": (
                    "NOT IMPLEMENTED: there is no per-tool provider policy in this build, so "
                    "this check has nothing to read and never excludes anything."
                ),
            },
            {
                "name": "runtime_status",
                "enforced": False,
                "detail": (
                    "NOT IMPLEMENTED: a registry tool is not yet linked to a specific runtime, "
                    "so runtime health cannot be checked per tool."
                ),
            },
        ],
    }
