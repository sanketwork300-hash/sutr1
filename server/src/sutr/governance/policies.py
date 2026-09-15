"""Versioned policies and the lifecycle they move through.

LLD §5.2: *"Policies are declarative, versioned, and deployed independently"*,
moving through **Draft → Review → Approved → Published → Active → Deprecated →
Archived**, with *"no admin may author and approve the same policy"*.

Three decisions shape this module.

**A version is immutable the moment it leaves DRAFT.** Everything downstream — a
review decision, an activation, an audit record — refers to what the version
*said*. A version editable after approval makes the approval a statement about
nothing.

**PUBLISHED and ACTIVE are different facts.** A version can be released without
being the one enforced, and exactly one version is active at a time. That is
what makes §5.2.11's *"roll back to last active version"* a single pointer move
rather than a reconciliation.

**Separation of duties is unconditional here.** Phase 6's registry gate enforces
four eyes only when a second approver exists, because a solo install would
otherwise be unable to publish a tool at all. A policy is different: the LLD
states the rule without qualification, and a tenant with no policies loses
nothing — an access policy with no active version contributes no rules. So this
refuses, and the message names the fix (ADR-058).
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, func, select

from sutr.common.errors import ConflictError, ForbiddenError, InvalidRequestError
from sutr.models.governance_policy import (
    ACTIVE,
    APPROVED,
    ARCHIVED,
    DEPRECATED,
    DRAFT,
    KIND_ACCESS,
    KINDS,
    LIFECYCLE,
    PUBLISHED,
    REVIEW,
    GovernancePolicy,
    GovernancePolicyVersion,
)

KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")

# Where a version may go next. Review can send it back to Draft, which is what
# "changes requested" is; everything else moves forward or retires.
TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({REVIEW, ARCHIVED}),
    REVIEW: frozenset({APPROVED, DRAFT}),
    APPROVED: frozenset({PUBLISHED, DRAFT}),
    PUBLISHED: frozenset({ACTIVE, DEPRECATED}),
    ACTIVE: frozenset({DEPRECATED}),
    DEPRECATED: frozenset({ARCHIVED, ACTIVE}),
    ARCHIVED: frozenset(),
}

# States a version may still be edited in. Exactly one.
EDITABLE = (DRAFT,)


class LifecycleError(ValueError):
    """A transition the policy lifecycle does not allow."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Rule:
    """One rule inside an access policy's document."""

    name: str
    effect: str
    subject: dict[str, str]
    resource: dict[str, str]
    action: str = "invoke"
    priority: int = 100
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "effect": self.effect,
            "subject": self.subject,
            "resource": self.resource,
            "action": self.action,
            "priority": self.priority,
            "description": self.description,
        }


def validate_document(kind: str, document: dict[str, Any]) -> dict[str, Any]:
    """Check a policy document before it can be stored.

    Validation happens at **write** time rather than at evaluation time. A
    malformed rule discovered while deciding a live request is a rule that has
    already failed open or closed once before anybody knew.
    """
    from sutr.provisioning import rules as abac

    if kind != KIND_ACCESS:
        # Compliance and operational policies are declarative prose for a
        # reviewer. Nothing evaluates them, and `describe()` says so rather
        # than implying an engine reads them.
        return {"statement": str(document.get("statement", "")).strip()}

    raw = document.get("rules")
    if not isinstance(raw, list) or not raw:
        raise InvalidRequestError(
            "An access policy needs a non-empty `rules` list. A policy that states nothing "
            "would activate and change no decision, which is worse than not existing."
        )
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise InvalidRequestError(f"Rule {index} is not an object.")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise InvalidRequestError(f"Rule {index} has no name; a decision has to name it.")
        if name in seen:
            raise InvalidRequestError(f"Rule '{name}' appears twice in this policy.")
        seen.add(name)
        effect = str(entry.get("effect", "")).strip().lower()
        if effect not in ("allow", "deny"):
            raise InvalidRequestError(f"Rule '{name}': effect must be allow or deny.")
        subject = {str(k): str(v) for k, v in (entry.get("subject") or {}).items()}
        resource = {str(k): str(v) for k, v in (entry.get("resource") or {}).items()}
        action = str(entry.get("action", "invoke"))
        # The same guards the standalone rule authoring applies, so a rule
        # cannot get in through a policy that would be refused on its own.
        abac.validate(subject, resource, action)
        cleaned.append(
            Rule(
                name=name,
                effect=effect,
                subject=subject,
                resource=resource,
                action=action,
                priority=int(entry.get("priority", 100)),
                description=str(entry.get("description", "")),
            ).as_dict()
        )
    return {"rules": cleaned}


# ── Policies ─────────────────────────────────────────────────────────────────


def get(session: Session, policy_id: uuid.UUID, org_id: uuid.UUID) -> GovernancePolicy | None:
    policy = session.get(GovernancePolicy, policy_id)
    if policy is None or policy.org_id != org_id:
        return None
    return policy


def list_policies(session: Session, *, org_id: uuid.UUID) -> list[GovernancePolicy]:
    return list(
        session.exec(
            select(GovernancePolicy)
            .where(GovernancePolicy.org_id == org_id)
            .order_by(col(GovernancePolicy.key))
        ).all()
    )


def create(
    session: Session,
    *,
    org_id: uuid.UUID,
    key: str,
    name: str,
    kind: str = KIND_ACCESS,
    description: str = "",
    document: dict[str, Any] | None = None,
    notes: str = "",
    created_by_user_id: uuid.UUID | None = None,
) -> tuple[GovernancePolicy, GovernancePolicyVersion]:
    """Create a policy and its first draft version. The caller commits."""
    if not KEY.match(key or ""):
        raise InvalidRequestError(
            "A policy key is 3–64 characters of lowercase letters, digits, hyphens and "
            "underscores. It is what an evaluation result refers to and is never renamed."
        )
    if kind not in KINDS:
        raise InvalidRequestError(f"Unknown policy kind '{kind}'. Known: {', '.join(KINDS)}.")
    existing = session.exec(
        select(GovernancePolicy)
        .where(GovernancePolicy.org_id == org_id)
        .where(GovernancePolicy.key == key)
    ).first()
    if existing is not None:
        raise ConflictError(f"A policy with the key '{key}' already exists.")

    policy = GovernancePolicy(
        org_id=org_id,
        key=key,
        name=name,
        description=description,
        kind=kind,
        created_by_user_id=created_by_user_id,
    )
    session.add(policy)
    session.flush()
    version = draft(
        session,
        policy,
        document=document or {},
        notes=notes,
        authored_by_user_id=created_by_user_id,
    )
    return policy, version


def draft(
    session: Session,
    policy: GovernancePolicy,
    *,
    document: dict[str, Any],
    notes: str = "",
    authored_by_user_id: uuid.UUID | None = None,
) -> GovernancePolicyVersion:
    """Open a new draft version. The caller commits."""
    open_draft = session.exec(
        select(GovernancePolicyVersion)
        .where(GovernancePolicyVersion.policy_id == policy.id)
        .where(GovernancePolicyVersion.state == DRAFT)
    ).first()
    if open_draft is not None:
        raise ConflictError(
            f"Version {open_draft.version} of this policy is still a draft. Edit it, or send it "
            "for review first — two open drafts would race to become the next version."
        )
    highest = session.exec(
        select(func.max(GovernancePolicyVersion.version)).where(
            GovernancePolicyVersion.policy_id == policy.id
        )
    ).one()
    version = GovernancePolicyVersion(
        policy_id=policy.id,
        org_id=policy.org_id,
        version=int(highest or 0) + 1,
        state=DRAFT,
        document_json=json.dumps(validate_document(policy.kind, document), sort_keys=True),
        notes=notes,
        authored_by_user_id=authored_by_user_id,
    )
    session.add(version)
    session.flush()
    policy.current_version = version.version
    policy.updated_at = _utcnow()
    session.add(policy)
    return version


def edit(
    session: Session,
    policy: GovernancePolicy,
    version: GovernancePolicyVersion,
    *,
    document: dict[str, Any],
    notes: str | None = None,
) -> GovernancePolicyVersion:
    if version.state not in EDITABLE:
        raise ConflictError(
            f"Version {version.version} is {version.state} and cannot be edited. Everything that "
            "refers to it — a review decision, an activation, an audit record — refers to what "
            "it said. Open a new draft instead."
        )
    version.document_json = json.dumps(validate_document(policy.kind, document), sort_keys=True)
    if notes is not None:
        version.notes = notes
    session.add(version)
    return version


# ── Lifecycle ────────────────────────────────────────────────────────────────


def check(source: str, target: str) -> None:
    if target not in LIFECYCLE:
        raise LifecycleError(f"'{target}' is not a policy lifecycle state.")
    if target not in TRANSITIONS.get(source, frozenset()):
        options = ", ".join(sorted(TRANSITIONS.get(source, frozenset()))) or "nothing"
        raise LifecycleError(
            f"A {source} policy version cannot become {target}. It can become: {options}."
        )


def transition(
    session: Session,
    policy: GovernancePolicy,
    version: GovernancePolicyVersion,
    target: str,
    *,
    actor_user_id: uuid.UUID | None,
    note: str = "",
) -> GovernancePolicyVersion:
    """Move a version along its lifecycle, enforcing separation of duties."""
    check(version.state, target)

    if target == APPROVED:
        if actor_user_id is not None and version.authored_by_user_id == actor_user_id:
            raise ForbiddenError(
                "You wrote this policy version, so you cannot approve it. Separation of duties "
                "(LLD §5.2.10) is unconditional for policies: another administrator has to "
                "approve it. Add one, or have an existing one review it."
            )
        version.approved_by_user_id = actor_user_id
        version.approved_at = _utcnow()

    now = _utcnow()
    if target == PUBLISHED:
        version.published_at = now
    if target == DEPRECATED:
        version.deprecated_at = now
        if policy.active_version == version.version:
            # A deprecated version stops being enforced. Leaving it active
            # would make "deprecated" a label rather than a state.
            policy.previous_active_version = policy.active_version
            policy.active_version = None
    if target == ARCHIVED:
        version.archived_at = now
    if note:
        version.decision_note = note

    version.state = target
    session.add(version)
    policy.updated_at = now
    session.add(policy)
    return version


def activate(
    session: Session,
    policy: GovernancePolicy,
    version: GovernancePolicyVersion,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> GovernancePolicyVersion:
    """Make this version the one being enforced.

    Activation is a pointer move, and the previous pointer is kept — which is
    exactly what §5.2.11's *"policy deploy failure — roll back to last active
    version"* needs. `rollback` is its inverse.
    """
    check(version.state, ACTIVE)
    previous = session.exec(
        select(GovernancePolicyVersion)
        .where(GovernancePolicyVersion.policy_id == policy.id)
        .where(GovernancePolicyVersion.state == ACTIVE)
    ).first()
    if previous is not None:
        previous.state = DEPRECATED
        previous.deprecated_at = _utcnow()
        session.add(previous)

    policy.previous_active_version = policy.active_version
    policy.active_version = version.version
    policy.updated_at = _utcnow()
    session.add(policy)

    version.state = ACTIVE
    version.activated_at = _utcnow()
    session.add(version)
    return version


def rollback(session: Session, policy: GovernancePolicy, *, reason: str = "") -> int | None:
    """Restore the previously active version. Returns the version restored.

    The fail-safe for a deployment that went wrong. It does not re-run
    validation on the restored version: it was active before, which is the
    strongest evidence available that it works.
    """
    target_version = policy.previous_active_version
    if target_version is None:
        raise ConflictError("This policy has no previous active version to roll back to.")
    restored = session.exec(
        select(GovernancePolicyVersion)
        .where(GovernancePolicyVersion.policy_id == policy.id)
        .where(GovernancePolicyVersion.version == target_version)
    ).first()
    if restored is None:
        raise ConflictError(f"Version {target_version} no longer exists.")

    current = session.exec(
        select(GovernancePolicyVersion)
        .where(GovernancePolicyVersion.policy_id == policy.id)
        .where(GovernancePolicyVersion.state == ACTIVE)
    ).first()
    if current is not None:
        current.state = DEPRECATED
        current.deprecated_at = _utcnow()
        current.decision_note = reason or "rolled back"
        session.add(current)

    restored.state = ACTIVE
    restored.activated_at = _utcnow()
    session.add(restored)
    policy.previous_active_version = policy.active_version
    policy.active_version = restored.version
    policy.updated_at = _utcnow()
    session.add(policy)
    return restored.version


# ── Reading ──────────────────────────────────────────────────────────────────


def versions_for(session: Session, policy_id: uuid.UUID) -> list[GovernancePolicyVersion]:
    return list(
        session.exec(
            select(GovernancePolicyVersion)
            .where(GovernancePolicyVersion.policy_id == policy_id)
            .order_by(desc(col(GovernancePolicyVersion.version)))
        ).all()
    )


def get_version(
    session: Session, policy_id: uuid.UUID, version: int
) -> GovernancePolicyVersion | None:
    return session.exec(
        select(GovernancePolicyVersion)
        .where(GovernancePolicyVersion.policy_id == policy_id)
        .where(GovernancePolicyVersion.version == version)
    ).first()


def active_rules(session: Session, *, org_id: uuid.UUID, action: str = "invoke") -> list[dict]:
    """Every rule from every ACTIVE access policy in this tenant.

    This is what makes a policy *deployed*: the decision point reads it. A
    version in any other state contributes nothing, which is what lets a draft
    be written without changing a single live decision.
    """
    rows = session.exec(
        select(GovernancePolicyVersion, GovernancePolicy)
        .join(GovernancePolicy, col(GovernancePolicyVersion.policy_id) == col(GovernancePolicy.id))
        .where(GovernancePolicyVersion.org_id == org_id)
        .where(GovernancePolicyVersion.state == ACTIVE)
        .where(GovernancePolicy.kind == KIND_ACCESS)
    ).all()
    collected: list[dict] = []
    for version, policy in rows:
        document = json.loads(version.document_json or "{}")
        for rule in document.get("rules", []):
            if rule.get("action", "invoke") != action:
                continue
            collected.append(
                {
                    **rule,
                    "policy_key": policy.key,
                    "policy_version": version.version,
                    "source": "policy",
                }
            )
    collected.sort(key=lambda rule: (rule.get("priority", 100), rule.get("name", "")))
    return collected


def serialize(policy: GovernancePolicy) -> dict[str, Any]:
    return {
        "id": str(policy.id),
        "key": policy.key,
        "name": policy.name,
        "description": policy.description,
        "kind": policy.kind,
        "current_version": policy.current_version or None,
        "active_version": policy.active_version,
        "previous_active_version": policy.previous_active_version,
        "created_at": policy.created_at.isoformat(),
        "updated_at": policy.updated_at.isoformat(),
    }


def serialize_version(version: GovernancePolicyVersion) -> dict[str, Any]:
    return {
        "version": version.version,
        "id": str(version.id),
        "state": version.state,
        "document": json.loads(version.document_json or "{}"),
        "notes": version.notes or None,
        "decision_note": version.decision_note or None,
        "authored_by_user_id": (
            str(version.authored_by_user_id) if version.authored_by_user_id else None
        ),
        "approved_by_user_id": (
            str(version.approved_by_user_id) if version.approved_by_user_id else None
        ),
        "approved_at": version.approved_at.isoformat() if version.approved_at else None,
        "activated_at": version.activated_at.isoformat() if version.activated_at else None,
        "editable": version.state in EDITABLE,
        "allowed_transitions": sorted(TRANSITIONS.get(version.state, frozenset())),
        "created_at": version.created_at.isoformat(),
    }


def describe() -> dict[str, Any]:
    return {
        "lifecycle": list(LIFECYCLE),
        "transitions": {state: sorted(targets) for state, targets in TRANSITIONS.items()},
        "kinds": [
            {
                "name": KIND_ACCESS,
                "evaluated": True,
                "detail": "Rules an ACTIVE version contributes to every authorization decision.",
            },
            {
                "name": "compliance",
                "evaluated": False,
                "detail": "Declarative prose for a reviewer. Nothing evaluates it.",
            },
            {
                "name": "operational",
                "evaluated": False,
                "detail": "Declarative prose for a reviewer. Nothing evaluates it.",
            },
        ],
        "separation_of_duties": (
            "Unconditional for policies: the author of a version may not approve it. Unlike the "
            "registry's change gate, there is no solo-install exemption — a tenant with no "
            "policies loses nothing, so the rule can be absolute."
        ),
    }
