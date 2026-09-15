"""The policy decision point: four authorization layers, evaluated in order.

LLD §4.3 shows *"AUTHORIZATION — four layers, evaluated in order"* as a figure,
and gives one worked example: *Finance role ∧ Organization=Bank-A ∧
Region=India ⇒ Allow Refund Tool*.

**The figure's own labels are not extractable from the condensed edition** —
the four boxes are an image, and the text layer carries only the caption and
the ABAC example. Rather than guess at four names and assert them as the LLD's,
the layers below are derived from what the document *does* say in text: it names
tenant isolation, RBAC, ABAC and per-tool policy as the platform's authorization
machinery, and Sutr already had three of the four. If the uncondensed LLD names
them differently, the mapping changes and the ordering does not (ADR-053).

    1. tenant     is the principal acting inside its own tenant?
    2. rbac       does the caller's role hold the permission?
    3. abac       do the attributes satisfy this tenant's rules?
    4. policy     the per-tool execution setting and approval requirement

Layers 1–3 are decided here. **Layer 4 is delegated** to `approvals/policy.py`,
which already implements it and returns something richer than allow/deny —
allow, deny, or require-approval — and is called by the enforcement point
immediately after this. The split is documented rather than hidden, and
`describe()` says so.

The order is not cosmetic. Each layer is cheaper than the next and refuses on
less information: a cross-tenant call must not reach a rule evaluation, and a
role check must not require loading a tenant's rule set.

A decision is always **explainable**. Every layer records what it concluded and
why, so a 403 can carry a reason a person can act on — which LLD §4.2's hot-path
table asks for in as many words: *"403 + policy reason"*.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from sutr.authz import PERMISSIONS, role_can
from sutr.provisioning import rules
from sutr.provisioning.identity import Principal

LAYER_TENANT = "tenant"
LAYER_RBAC = "rbac"
LAYER_ABAC = "abac"
LAYER_POLICY = "policy"

# In order. The fourth is evaluated by the enforcement point, not here.
LAYERS = (LAYER_TENANT, LAYER_RBAC, LAYER_ABAC, LAYER_POLICY)

ALLOW = "allow"
DENY = "deny"
# The layer had nothing to say. Distinct from allow: three layers with no
# opinion is not the same as three layers approving.
ABSTAIN = "abstain"


@dataclass
class LayerResult:
    layer: str
    effect: str
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "effect": self.effect,
            "reason": self.reason or None,
            "detail": self.detail,
        }


@dataclass
class Decision:
    allowed: bool
    principal: str
    resource: dict[str, Any]
    action: str
    layers: list[LayerResult] = field(default_factory=list)
    # The layer that refused, when one did.
    denied_by: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "principal": self.principal,
            "action": self.action,
            "resource": self.resource,
            "denied_by": self.denied_by,
            "reason": self.reason or None,
            "layers": [layer.as_dict() for layer in self.layers],
            "layer_order": list(LAYERS),
            "note": (
                "Layers 1–3 are decided by the policy decision point. Layer 4 — the per-tool "
                "execution policy and its approval requirement — is evaluated by the "
                "enforcement point immediately after this."
            ),
        }


def _tenant_layer(principal: Principal, org_id: uuid.UUID) -> LayerResult:
    if principal.org_id != org_id:
        return LayerResult(
            layer=LAYER_TENANT,
            effect=DENY,
            reason="The caller is acting outside their own organization.",
            detail={"principal_org": str(principal.org_id), "resource_org": str(org_id)},
        )
    return LayerResult(layer=LAYER_TENANT, effect=ALLOW, reason="Same tenant.")


def _rbac_layer(principal: Principal, permission: str | None) -> LayerResult:
    if permission is None:
        return LayerResult(
            layer=LAYER_RBAC, effect=ABSTAIN, reason="No permission was named for this action."
        )
    if permission not in PERMISSIONS:
        return LayerResult(
            layer=LAYER_RBAC, effect=DENY, reason=f"Unknown permission '{permission}'."
        )
    if principal.role is None:
        # An API key carries no role — that has always been true and is
        # documented in `authz.ensure_agent_can`. The layer abstains rather
        # than inventing one, and the ABAC layer is where a tenant closes this
        # gap by giving the agent attributes.
        return LayerResult(
            layer=LAYER_RBAC,
            effect=ABSTAIN,
            reason=(
                "This principal carries no role — API keys and services are not role-checked. "
                "Use an access rule to constrain it."
            ),
            detail={"principal_kind": principal.kind},
        )
    if not role_can(principal.role, permission):
        return LayerResult(
            layer=LAYER_RBAC,
            effect=DENY,
            reason=f"The role '{principal.role}' does not hold '{permission}'.",
            detail={"role": principal.role, "permission": permission},
        )
    return LayerResult(
        layer=LAYER_RBAC,
        effect=ALLOW,
        reason=f"The role '{principal.role}' holds '{permission}'.",
        detail={"role": principal.role, "permission": permission},
    )


def _abac_layer(
    session: Session,
    principal: Principal,
    resource: dict[str, Any],
    action: str,
) -> LayerResult:
    subject = {
        **principal.attributes,
        "principal_kind": principal.kind,
    }
    if principal.role:
        # Written last on purpose: the platform role is authoritative, so a
        # tenant cannot widen its own access by putting `role: owner` in an
        # identity's attributes. An API key carries no platform role, which is
        # why `role` on an agent is the tenant's own business attribute.
        subject["role"] = principal.role
    outcome = rules.evaluate(
        session, org_id=principal.org_id, subject=subject, resource=resource, action=action
    )
    if outcome.effect is None:
        return LayerResult(
            layer=LAYER_ABAC,
            effect=ABSTAIN,
            reason=(
                "No access rule applies."
                if outcome.evaluated
                else "This organization has no access rules."
            ),
            detail=outcome.as_dict(),
        )
    if outcome.denied:
        names = ", ".join(match.name for match in outcome.matches)
        return LayerResult(
            layer=LAYER_ABAC,
            effect=DENY,
            reason=f"Refused by access rule: {names}.",
            detail=outcome.as_dict(),
        )
    return LayerResult(
        layer=LAYER_ABAC,
        effect=ALLOW,
        reason=f"Allowed by access rule: {outcome.matches[0].name}.",
        detail=outcome.as_dict(),
    )


def authorize(
    session: Session,
    *,
    principal: Principal,
    org_id: uuid.UUID,
    resource: dict[str, Any],
    action: str = "invoke",
    permission: str | None = None,
) -> Decision:
    """Evaluate layers 1–3. The first denial stops the chain.

    Stopping early is deliberate: a cross-tenant caller must not cause this
    tenant's rule set to be loaded, and the layers that did not run are
    recorded as not having run rather than as having agreed.
    """
    layers: list[LayerResult] = []

    tenant = _tenant_layer(principal, org_id)
    layers.append(tenant)
    if tenant.effect == DENY:
        return Decision(
            allowed=False,
            principal=principal.urn,
            resource=resource,
            action=action,
            layers=layers,
            denied_by=LAYER_TENANT,
            reason=tenant.reason,
        )

    rbac = _rbac_layer(principal, permission)
    layers.append(rbac)
    if rbac.effect == DENY:
        return Decision(
            allowed=False,
            principal=principal.urn,
            resource=resource,
            action=action,
            layers=layers,
            denied_by=LAYER_RBAC,
            reason=rbac.reason,
        )

    abac = _abac_layer(session, principal, resource, action)
    layers.append(abac)
    if abac.effect == DENY:
        return Decision(
            allowed=False,
            principal=principal.urn,
            resource=resource,
            action=action,
            layers=layers,
            denied_by=LAYER_ABAC,
            reason=abac.reason,
        )

    return Decision(
        allowed=True,
        principal=principal.urn,
        resource=resource,
        action=action,
        layers=layers,
        reason="No layer refused.",
    )


def describe() -> dict[str, Any]:
    return {
        "layers": [
            {
                "order": 1,
                "name": LAYER_TENANT,
                "decided_here": True,
                "detail": "Is the principal acting inside its own tenant?",
            },
            {
                "order": 2,
                "name": LAYER_RBAC,
                "decided_here": True,
                "detail": (
                    "Does the caller's role hold the permission? API keys and services carry "
                    "no role and this layer abstains for them."
                ),
            },
            {
                "order": 3,
                "name": LAYER_ABAC,
                "decided_here": True,
                "detail": (
                    "Do the principal's attributes and the resource satisfy this tenant's "
                    "access rules? Deny wins; no matching rule is no opinion."
                ),
            },
            {
                "order": 4,
                "name": LAYER_POLICY,
                "decided_here": False,
                "detail": (
                    "The per-tool execution setting and its approval requirement, evaluated by "
                    "approvals/policy.py at the enforcement point immediately after."
                ),
            },
        ],
        "effects": [ALLOW, DENY, ABSTAIN],
        "source": (
            "The LLD names four ordered layers in a figure whose labels are not extractable "
            "from the condensed edition. These four are derived from what it states in text "
            "(ADR-053)."
        ),
    }
