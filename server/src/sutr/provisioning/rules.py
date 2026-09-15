"""Attribute-based rules, matched and explained.

The LLD's example (§4.3.3) is the shape this has to express:

    Finance role ∧ Organization=Bank-A ∧ Region=India ⇒ Allow Refund Tool

A rule is a conjunction of matchers over the **subject** (the acting
principal's role and attributes), the **resource** (which integration, which
tool, which category) and an **action**. Every matcher that is present must
match; a matcher that is absent matches anything, because a rule that had to
name every attribute would need rewriting each time one was added.

Three properties, each a decision:

**Deny wins, unconditionally.** A tenant that wrote a deny rule decided
something, and an allow rule elsewhere must not quietly overrule it. Priority
orders which rule is *reported*; it never changes the outcome.

**No rules means no opinion.** A tenant with no ABAC rules keeps the behaviour
they had before this layer existed. A layer that starts denying the day it ships
is a layer nobody turns on.

**Every match is explainable.** A decision names the rule and the matchers that
fired. "Denied by policy" is a sentence nobody can act on.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.models.access_rule import EFFECT_DENY, EFFECTS, AccessRule

NAME = re.compile(r"^[a-z0-9][a-z0-9 _-]{1,62}[a-z0-9]$")

# Matcher keys the resource side understands. Declared so a typo in a rule is
# a refusal at write time rather than a rule that silently never matches —
# which is the failure mode that makes authorization layers untrustworthy.
RESOURCE_KEYS = ("integration_id", "tool_name", "category", "provider_org_id", "region")
# The subject side is open: `role` is ours, everything else is whatever the
# tenant put on the agent identity.
SUBJECT_RESERVED = ("role", "principal_kind")

ACTIONS = ("invoke", "install", "read")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Match:
    """One rule that fired, and what made it fire.

    `source` distinguishes a standalone access rule from one contributed by an
    ACTIVE governance policy version. Both are real rules and both bind; a
    reader deciding whether to argue with a refusal needs to know which
    document to go and read.
    """

    name: str
    effect: str
    priority: int
    matched: dict[str, str]
    source: str = "rule"
    rule_id: str | None = None
    policy_key: str | None = None
    policy_version: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.name,
            "effect": self.effect,
            "priority": self.priority,
            "matched": self.matched,
            "source": self.source,
            "rule_id": self.rule_id,
            "policy_key": self.policy_key,
            "policy_version": self.policy_version,
        }


@dataclass
class RuleOutcome:
    """What the rule set concluded, and which rules said so."""

    effect: str | None
    matches: list[Match] = field(default_factory=list)
    evaluated: int = 0

    @property
    def denied(self) -> bool:
        return self.effect == EFFECT_DENY

    def as_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "rules_evaluated": self.evaluated,
            "matches": [match.as_dict() for match in self.matches],
        }


def _normalize(values: dict[str, Any]) -> dict[str, str]:
    """Lowercased, trimmed, and with empty values dropped.

    An empty matcher value is dropped rather than kept: a rule saying
    `integration_id: ""` matches nothing at all, and a rule that silently never
    matches is the failure this layer must not have. Dropping it turns the rule
    into "any integration", which is at least a policy somebody can read — and
    if that leaves the rule matching everything, `validate` refuses it.
    """
    return {
        str(key).strip().lower(): str(value).strip().lower()
        for key, value in (values or {}).items()
        if value is not None and str(value).strip()
    }


def _matches(matcher: dict[str, str], facts: dict[str, str]) -> dict[str, str] | None:
    """Every matcher entry must be satisfied. Returns what matched, or None."""
    matched: dict[str, str] = {}
    for key, expected in matcher.items():
        actual = facts.get(key)
        if actual is None or actual != expected:
            return None
        matched[key] = actual
    return matched


def evaluate(
    session: Session,
    *,
    org_id: uuid.UUID,
    subject: dict[str, Any],
    resource: dict[str, Any],
    action: str = "invoke",
) -> RuleOutcome:
    """Run this tenant's rules. Deny wins; no rules is no opinion.

    Two sources, one evaluation: standalone access rules, and the rules
    contributed by every **ACTIVE** governance policy version (LLD §5.2 —
    policies are *"deployed independently"*, and this is what deployed means).
    Deny wins across both, so a governance policy cannot be overruled by a
    standalone allow, and vice versa.
    """
    # Imported by full path so the boundary analysis sees `governance.policies`
    # — the shared half — rather than the control-plane `governance` package.
    from sutr.governance.policies import active_rules as policy_rules_for

    rows = session.exec(
        select(AccessRule)
        .where(AccessRule.org_id == org_id)
        .where(AccessRule.enabled == True)  # noqa: E712
        .where(AccessRule.action == action)
        .order_by(col(AccessRule.priority), col(AccessRule.name))
    ).all()
    policy_rules = policy_rules_for(session, org_id=org_id, action=action)
    if not rows and not policy_rules:
        return RuleOutcome(effect=None, evaluated=0)

    subject_facts = _normalize(subject)
    resource_facts = _normalize(resource)
    matches: list[Match] = []
    for rule in rows:
        subject_matcher = _normalize(json.loads(rule.subject_json or "{}"))
        resource_matcher = _normalize(json.loads(rule.resource_json or "{}"))
        subject_hit = _matches(subject_matcher, subject_facts)
        if subject_hit is None:
            continue
        resource_hit = _matches(resource_matcher, resource_facts)
        if resource_hit is None:
            continue
        matches.append(
            Match(
                name=rule.name,
                effect=rule.effect,
                priority=rule.priority,
                matched={**subject_hit, **resource_hit},
                source="rule",
                rule_id=str(rule.id),
            )
        )
    for rule in policy_rules:
        subject_hit = _matches(_normalize(rule.get("subject") or {}), subject_facts)
        if subject_hit is None:
            continue
        resource_hit = _matches(_normalize(rule.get("resource") or {}), resource_facts)
        if resource_hit is None:
            continue
        matches.append(
            Match(
                name=rule["name"],
                effect=rule["effect"],
                priority=int(rule.get("priority", 100)),
                matched={**subject_hit, **resource_hit},
                source="policy",
                policy_key=rule.get("policy_key"),
                policy_version=rule.get("policy_version"),
            )
        )
    matches.sort(key=lambda match: (match.priority, match.name))
    evaluated = len(rows) + len(policy_rules)

    if not matches:
        # Rules exist but none applies. Still no opinion: a rule set about
        # refunds says nothing about invoices, and reading its silence as a
        # denial would make every new tool unusable until somebody wrote a rule
        # for it.
        return RuleOutcome(effect=None, matches=[], evaluated=evaluated)

    denials = [match for match in matches if match.effect == EFFECT_DENY]
    if denials:
        return RuleOutcome(effect=EFFECT_DENY, matches=denials, evaluated=evaluated)
    return RuleOutcome(effect=matches[0].effect, matches=matches, evaluated=evaluated)


# ── Authoring ────────────────────────────────────────────────────────────────


def validate(subject: dict[str, Any], resource: dict[str, Any], action: str) -> None:
    if action not in ACTIONS:
        raise InvalidRequestError(f"Unknown action '{action}'. Known: {', '.join(ACTIONS)}.")
    if not subject and not resource:
        raise InvalidRequestError(
            "A rule with no subject and no resource matcher matches everything, which is a "
            "policy nobody can reason about. Name at least one."
        )
    unknown = sorted(set(_normalize(resource)) - set(RESOURCE_KEYS))
    if unknown:
        raise InvalidRequestError(
            f"Unknown resource attribute(s): {', '.join(unknown)}. Known: "
            f"{', '.join(RESOURCE_KEYS)}. A misspelled matcher would never match, and a rule "
            "that never matches is worse than no rule."
        )


def create(
    session: Session,
    *,
    org_id: uuid.UUID,
    name: str,
    effect: str,
    subject: dict[str, Any] | None = None,
    resource: dict[str, Any] | None = None,
    action: str = "invoke",
    priority: int = 100,
    description: str = "",
    created_by_user_id: uuid.UUID | None = None,
) -> AccessRule:
    if effect not in EFFECTS:
        raise InvalidRequestError(f"Unknown effect '{effect}'. Known: {', '.join(EFFECTS)}.")
    if not NAME.match((name or "").lower()):
        raise InvalidRequestError(
            "A rule name is 3–64 characters of letters, digits, spaces, - and _."
        )
    # Normalised before validation, so a matcher that is empty only after
    # trimming is caught here rather than becoming a rule that never fires.
    subject_matcher = _normalize(subject or {})
    resource_matcher = _normalize(resource or {})
    validate(subject_matcher, resource_matcher, action)
    existing = session.exec(
        select(AccessRule).where(AccessRule.org_id == org_id).where(AccessRule.name == name)
    ).first()
    if existing is not None:
        raise ConflictError(f"A rule called '{name}' already exists.")

    rule = AccessRule(
        org_id=org_id,
        name=name,
        description=description,
        effect=effect,
        priority=priority,
        subject_json=json.dumps(subject_matcher, sort_keys=True),
        resource_json=json.dumps(resource_matcher, sort_keys=True),
        action=action,
        created_by_user_id=created_by_user_id,
    )
    session.add(rule)
    session.flush()
    return rule


def get(session: Session, rule_id: uuid.UUID, org_id: uuid.UUID) -> AccessRule | None:
    rule = session.get(AccessRule, rule_id)
    if rule is None or rule.org_id != org_id:
        return None
    return rule


def list_rules(session: Session, *, org_id: uuid.UUID) -> list[AccessRule]:
    return list(
        session.exec(
            select(AccessRule)
            .where(AccessRule.org_id == org_id)
            .order_by(col(AccessRule.priority), col(AccessRule.name))
        ).all()
    )


def set_enabled(session: Session, rule: AccessRule, enabled: bool) -> AccessRule:
    rule.enabled = enabled
    rule.updated_at = _utcnow()
    session.add(rule)
    return rule


def delete(session: Session, rule: AccessRule) -> None:
    session.delete(rule)


def serialize(rule: AccessRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "name": rule.name,
        "description": rule.description,
        "effect": rule.effect,
        "priority": rule.priority,
        "subject": json.loads(rule.subject_json or "{}"),
        "resource": json.loads(rule.resource_json or "{}"),
        "action": rule.action,
        "enabled": rule.enabled,
        "created_at": rule.created_at.isoformat(),
    }
