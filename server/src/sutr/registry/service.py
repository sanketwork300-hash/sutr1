"""The Registry's operations: register, version, gate, publish, retire.

This is the authoritative side of LLD §3.7. Three things about it are worth
stating before the code, because each shapes every function below.

**Nothing here reads the marketplace.** The dependency runs one way: registry →
events → projection. A registry function that consulted a listing would make
the storefront an input to the record it is derived from.

**Gated changes do not take effect when requested.** Entering review,
publishing, changing visibility and changing price all produce a
`RegistryChangeRequest` and change nothing until somebody decides it. The
alternative — apply now, ask later — is not a gate.

**Four eyes where four eyes are possible.** A requester may not decide their own
change *when the organization has another eligible approver*. A solo install
would otherwise be unable to publish anything, which is not governance, it is a
deadlock. When a self-decision happens it is recorded as one, so the audit trail
says what actually occurred rather than implying a second person was involved.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, col, desc, select

from sutr.authz import role_can
from sutr.common.errors import ConflictError, ForbiddenError, InvalidRequestError, NotFoundError
from sutr.models.org_membership import OrgMembership
from sutr.models.registry_change_request import (
    KIND_LIFECYCLE,
    KIND_PRICING,
    KIND_VISIBILITY,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_WITHDRAWN,
    RegistryChangeRequest,
)
from sutr.models.registry_tool import (
    ARCHIVED,
    DEPRECATED,
    PUBLISHED,
    VISIBILITIES,
    RegistryTool,
)
from sutr.registry import events, lifecycle, pricing, trust, versions

# The permission a decision needs. Deciding a governance request is the same
# kind of act as deciding an approval request, so it holds the same permission
# rather than inventing a parallel one.
DECIDE_PERMISSION = "approvals:decide"

# Tool keys are addresses: they end up in URLs, in subscriptions and in a
# marketplace listing, and they are never renamed.
TOOL_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")

# Fields a provider may edit freely. Visibility and pricing are absent on
# purpose — those go through the gate.
EDITABLE = ("name", "summary", "description", "category", "tags", "regions", "compliance")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TransitionOutcome:
    """What happened when a transition was asked for."""

    applied: bool
    state: str
    change_request: RegistryChangeRequest | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "state": self.state,
            "change_request_id": (
                str(self.change_request.id) if self.change_request is not None else None
            ),
            "pending_reason": (
                None
                if self.applied
                else "This transition needs governance approval before it takes effect."
            ),
        }


# ── Lookup ───────────────────────────────────────────────────────────────────


def get(session: Session, tool_id: uuid.UUID, org_id: uuid.UUID) -> RegistryTool | None:
    tool = session.get(RegistryTool, tool_id)
    if tool is None or tool.org_id != org_id:
        # One answer for "no such tool" and "another tenant's tool".
        return None
    return tool


def get_by_key(session: Session, org_id: uuid.UUID, tool_key: str) -> RegistryTool | None:
    return session.exec(
        select(RegistryTool)
        .where(RegistryTool.org_id == org_id)
        .where(RegistryTool.tool_key == tool_key)
    ).first()


def list_tools(
    session: Session,
    *,
    org_id: uuid.UUID,
    lifecycle_state: str | None = None,
    visibility: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[RegistryTool]:
    statement = select(RegistryTool).where(RegistryTool.org_id == org_id)
    if lifecycle_state:
        statement = statement.where(RegistryTool.lifecycle_state == lifecycle_state)
    if visibility:
        statement = statement.where(RegistryTool.visibility == visibility)
    statement = statement.order_by(desc(col(RegistryTool.created_at)))
    return list(session.exec(statement.offset(offset).limit(limit)).all())


# ── Registration and edits ───────────────────────────────────────────────────


def register(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_key: str,
    name: str,
    summary: str = "",
    description: str = "",
    category: str = "other",
    tags: list[str] | None = None,
    regions: list[str] | None = None,
    compliance: list[str] | None = None,
    project_id: uuid.UUID | None = None,
    integration_id: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> RegistryTool:
    """Create the record. Starts at DRAFT, private. The caller commits."""
    if not TOOL_KEY.match(tool_key or ""):
        raise InvalidRequestError(
            "A tool key is 3–64 characters of lowercase letters, digits, hyphens and "
            "underscores, starting and ending with a letter or digit. It becomes part of a URL "
            "and is never renamed."
        )
    if get_by_key(session, org_id, tool_key) is not None:
        raise ConflictError(
            f"'{tool_key}' is already registered. A tool key identifies one tool forever, so it "
            "is not reused even after archiving."
        )
    tool = RegistryTool(
        org_id=org_id,
        tool_key=tool_key,
        name=name,
        summary=summary,
        description=description,
        category=category,
        tags_json=json.dumps(sorted(set(tags or []))),
        regions_json=json.dumps(sorted(set(regions or []))),
        compliance_json=json.dumps(sorted(set(compliance or []))),
        project_id=project_id,
        integration_id=integration_id,
        created_by_user_id=created_by_user_id,
    )
    session.add(tool)
    session.flush()
    events.registered(
        session,
        org_id=org_id,
        tool_id=tool.id,
        tool_key=tool.tool_key,
        name=tool.name,
        category=tool.category,
        visibility=tool.visibility,
    )
    return tool


def update(session: Session, tool: RegistryTool, changes: dict[str, Any]) -> list[str]:
    """Apply free-form metadata edits. Returns the field names that changed."""
    unknown = sorted(set(changes) - set(EDITABLE))
    if unknown:
        raise InvalidRequestError(
            f"Not editable here: {', '.join(unknown)}. Visibility and pricing changes go through "
            "a change request, and lifecycle moves through a transition."
        )
    changed: list[str] = []
    for field in ("name", "summary", "description", "category"):
        if field in changes and changes[field] != getattr(tool, field):
            setattr(tool, field, changes[field])
            changed.append(field)
    for field in ("tags", "regions", "compliance"):
        if field not in changes:
            continue
        column = f"{field}_json"
        value = json.dumps(sorted(set(changes[field] or [])))
        if value != getattr(tool, column):
            setattr(tool, column, value)
            changed.append(field)
    if changed:
        tool.updated_at = _utcnow()
        session.add(tool)
        events.updated(
            session,
            org_id=tool.org_id,
            tool_id=tool.id,
            tool_key=tool.tool_key,
            changed=changed,
            lifecycle_state=tool.lifecycle_state,
            visibility=tool.visibility,
        )
    return changed


# ── Lifecycle ────────────────────────────────────────────────────────────────


def transition(
    session: Session,
    tool: RegistryTool,
    target: str,
    *,
    reason: str = "",
    requested_by_user_id: uuid.UUID | None = None,
) -> TransitionOutcome:
    """Move a tool along the lifecycle, through governance where required."""
    step = lifecycle.check(tool.lifecycle_state, target)
    if step.gated:
        request = _open_change_request(
            session,
            tool,
            kind=KIND_LIFECYCLE,
            current={"lifecycle_state": tool.lifecycle_state},
            requested={"lifecycle_state": target},
            reason=reason,
            requested_by_user_id=requested_by_user_id,
        )
        return TransitionOutcome(applied=False, state=tool.lifecycle_state, change_request=request)
    _apply_state(session, tool, target, reason=reason)
    return TransitionOutcome(applied=True, state=tool.lifecycle_state)


def _apply_state(session: Session, tool: RegistryTool, target: str, *, reason: str = "") -> None:
    tool.lifecycle_state = target
    tool.state_reason = reason
    tool.updated_at = _utcnow()
    if target == PUBLISHED:
        tool.published_version = tool.current_version or None
        tool.published_at = tool.published_at or _utcnow()
        version = (
            versions.get(session, tool.id, tool.published_version)
            if tool.published_version
            else None
        )
        if version is not None and not version.was_published:
            version.was_published = True
            session.add(version)
    if target == DEPRECATED:
        tool.deprecated_at = _utcnow()
    if target == ARCHIVED:
        tool.archived_at = _utcnow()
    session.add(tool)

    if target == PUBLISHED:
        events.published(
            session,
            org_id=tool.org_id,
            tool_id=tool.id,
            tool_key=tool.tool_key,
            version=tool.published_version,
            visibility=tool.visibility,
        )
    elif target == DEPRECATED:
        events.deprecated(
            session,
            org_id=tool.org_id,
            tool_id=tool.id,
            tool_key=tool.tool_key,
            note=tool.deprecation_note or reason,
        )
    elif target == ARCHIVED:
        events.archived(session, org_id=tool.org_id, tool_id=tool.id, tool_key=tool.tool_key)
    else:
        events.updated(
            session,
            org_id=tool.org_id,
            tool_id=tool.id,
            tool_key=tool.tool_key,
            changed=["lifecycle_state"],
            lifecycle_state=tool.lifecycle_state,
            visibility=tool.visibility,
        )


def deprecate(
    session: Session,
    tool: RegistryTool,
    *,
    note: str,
    requested_by_user_id: uuid.UUID | None = None,
) -> TransitionOutcome:
    """Mark a tool as on its way out, with a note saying what to use instead."""
    if not note.strip():
        raise InvalidRequestError(
            "Deprecation needs a note. A tool that stops being recommended without saying why "
            "or what replaces it leaves every consumer to guess."
        )
    tool.deprecation_note = note
    session.add(tool)
    return transition(
        session, tool, DEPRECATED, reason=note, requested_by_user_id=requested_by_user_id
    )


def archive(
    session: Session, tool: RegistryTool, *, requested_by_user_id: uuid.UUID | None = None
) -> TransitionOutcome:
    return transition(session, tool, ARCHIVED, requested_by_user_id=requested_by_user_id)


# ── Governance gate ──────────────────────────────────────────────────────────


def _open_change_request(
    session: Session,
    tool: RegistryTool,
    *,
    kind: str,
    current: dict[str, Any],
    requested: dict[str, Any],
    reason: str,
    requested_by_user_id: uuid.UUID | None,
) -> RegistryChangeRequest:
    existing = session.exec(
        select(RegistryChangeRequest)
        .where(RegistryChangeRequest.tool_id == tool.id)
        .where(RegistryChangeRequest.kind == kind)
        .where(RegistryChangeRequest.status == STATUS_PENDING)
    ).first()
    if existing is not None:
        raise ConflictError(
            f"A {kind} change is already pending for this tool. Decide or withdraw it first — two "
            "pending changes to the same thing would race each other."
        )
    request = RegistryChangeRequest(
        org_id=tool.org_id,
        tool_id=tool.id,
        kind=kind,
        current_json=json.dumps(current, sort_keys=True),
        requested_json=json.dumps(requested, sort_keys=True),
        reason=reason,
        requested_by_user_id=requested_by_user_id,
    )
    session.add(request)
    session.flush()
    return request


def request_visibility(
    session: Session,
    tool: RegistryTool,
    *,
    visibility: str,
    reason: str = "",
    requested_by_user_id: uuid.UUID | None = None,
) -> RegistryChangeRequest:
    if visibility not in VISIBILITIES:
        raise InvalidRequestError(
            f"Unknown visibility '{visibility}'. Known: {', '.join(VISIBILITIES)}."
        )
    if visibility == tool.visibility:
        raise ConflictError(f"This tool is already {visibility}.")
    return _open_change_request(
        session,
        tool,
        kind=KIND_VISIBILITY,
        current={"visibility": tool.visibility},
        requested={"visibility": visibility},
        reason=reason,
        requested_by_user_id=requested_by_user_id,
    )


def request_pricing(
    session: Session,
    tool: RegistryTool,
    *,
    price: dict[str, Any],
    reason: str = "",
    requested_by_user_id: uuid.UUID | None = None,
) -> RegistryChangeRequest:
    """Ask to change the price. Validated now, applied only if approved.

    Validating at request time rather than at decision time is deliberate: an
    approver should not be the one who discovers the price was malformed, and a
    request that cannot be applied should never reach them.
    """
    pricing.validate(
        model=price.get("model", ""),
        amount_micros=int(price.get("amount_micros", 0)),
        unit=price.get("unit", "call"),
        currency=str(price.get("currency", "USD")).upper(),
        free_allowance=int(price.get("free_allowance", 0)),
    )
    live = pricing.current(session, tool.id)
    return _open_change_request(
        session,
        tool,
        kind=KIND_PRICING,
        current=pricing.serialize(live) or {"model": None},
        requested=price,
        reason=reason,
        requested_by_user_id=requested_by_user_id,
    )


def eligible_approvers(
    session: Session, org_id: uuid.UUID, *, excluding: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Members who hold the decide permission, optionally excluding one person."""
    memberships = session.exec(select(OrgMembership).where(OrgMembership.org_id == org_id)).all()
    return [
        membership.user_id
        for membership in memberships
        if role_can(membership.role, DECIDE_PERMISSION) and membership.user_id != excluding
    ]


def decide(
    session: Session,
    request: RegistryChangeRequest,
    *,
    approve: bool,
    decided_by_user_id: uuid.UUID | None,
    note: str = "",
) -> RegistryChangeRequest:
    """Approve or reject a pending change, applying it if approved."""
    if request.status != STATUS_PENDING:
        raise ConflictError(f"This request was already {request.status}.")
    if (
        decided_by_user_id is not None
        and request.requested_by_user_id == decided_by_user_id
        and eligible_approvers(session, request.org_id, excluding=decided_by_user_id)
    ):
        raise ForbiddenError(
            "You asked for this change, so somebody else has to decide it. Another member of "
            "this organization can."
        )

    tool = session.get(RegistryTool, request.tool_id)
    if tool is None:
        raise NotFoundError("The tool this request refers to no longer exists.")

    request.status = STATUS_APPROVED if approve else STATUS_REJECTED
    request.decision_note = note
    request.decided_by_user_id = decided_by_user_id
    request.decided_at = _utcnow()
    session.add(request)

    if not approve:
        return request

    events.approved(
        session,
        org_id=tool.org_id,
        tool_id=tool.id,
        change_request_id=request.id,
        kind=request.kind,
    )
    requested = json.loads(request.requested_json or "{}")
    if request.kind == KIND_LIFECYCLE:
        _apply_state(session, tool, requested["lifecycle_state"], reason=request.reason)
    elif request.kind == KIND_VISIBILITY:
        tool.visibility = requested["visibility"]
        tool.updated_at = _utcnow()
        session.add(tool)
        events.updated(
            session,
            org_id=tool.org_id,
            tool_id=tool.id,
            tool_key=tool.tool_key,
            changed=["visibility"],
            lifecycle_state=tool.lifecycle_state,
            visibility=tool.visibility,
        )
    elif request.kind == KIND_PRICING:
        price = pricing.set_price(
            session,
            tool_id=tool.id,
            org_id=tool.org_id,
            model=requested.get("model", "free"),
            amount_micros=int(requested.get("amount_micros", 0)),
            unit=requested.get("unit", "call"),
            currency=str(requested.get("currency", "USD")).upper(),
            free_allowance=int(requested.get("free_allowance", 0)),
            notes=requested.get("notes", ""),
            created_by_user_id=decided_by_user_id,
        )
        events.pricing_updated(
            session,
            org_id=tool.org_id,
            tool_id=tool.id,
            pricing_id=price.id,
            model=price.model,
            amount_micros=price.amount_micros,
            currency=price.currency,
            unit=price.unit,
        )
    return request


def withdraw(
    session: Session, request: RegistryChangeRequest, *, user_id: uuid.UUID | None
) -> RegistryChangeRequest:
    if request.status != STATUS_PENDING:
        raise ConflictError(f"This request was already {request.status}.")
    request.status = STATUS_WITHDRAWN
    request.decided_by_user_id = user_id
    request.decided_at = _utcnow()
    session.add(request)
    return request


def list_change_requests(
    session: Session,
    *,
    org_id: uuid.UUID,
    tool_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[RegistryChangeRequest]:
    statement = select(RegistryChangeRequest).where(RegistryChangeRequest.org_id == org_id)
    if tool_id is not None:
        statement = statement.where(RegistryChangeRequest.tool_id == tool_id)
    if status:
        statement = statement.where(RegistryChangeRequest.status == status)
    return list(session.exec(statement.order_by(desc(col(RegistryChangeRequest.created_at)))).all())


def get_change_request(
    session: Session, request_id: uuid.UUID, org_id: uuid.UUID
) -> RegistryChangeRequest | None:
    request = session.get(RegistryChangeRequest, request_id)
    if request is None or request.org_id != org_id:
        return None
    return request


# ── Serialization ────────────────────────────────────────────────────────────


def serialize(session: Session, tool: RegistryTool, *, detail: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(tool.id),
        "tool_key": tool.tool_key,
        "name": tool.name,
        "summary": tool.summary,
        "category": tool.category,
        "tags": json.loads(tool.tags_json or "[]"),
        # Said in the payload, not only in a docstring: these are the
        # provider's claims and nothing has checked them.
        "regions": json.loads(tool.regions_json or "[]"),
        "compliance": json.loads(tool.compliance_json or "[]"),
        "declared_by_provider": ["regions", "compliance"],
        "visibility": tool.visibility,
        "lifecycle_state": tool.lifecycle_state,
        "progress": lifecycle.progress(tool.lifecycle_state),
        "state_reason": tool.state_reason or None,
        "current_version": tool.current_version or None,
        "published_version": tool.published_version,
        "project_id": str(tool.project_id) if tool.project_id else None,
        "integration_id": tool.integration_id,
        "created_at": tool.created_at.isoformat(),
        "updated_at": tool.updated_at.isoformat(),
    }
    if detail:
        payload["description"] = tool.description
        payload["deprecation_note"] = tool.deprecation_note or None
        payload["allowed_transitions"] = [
            {
                "to": target,
                "requires_approval": lifecycle.requires_approval(tool.lifecycle_state, target),
            }
            for target in lifecycle.allowed_targets(tool.lifecycle_state)
        ]
        payload["versions"] = [
            versions.serialize(version) for version in versions.list_for_tool(session, tool.id)
        ]
        payload["pricing"] = pricing.serialize(pricing.current(session, tool.id))
        payload["trust"] = trust.compute(session, tool).as_dict()
    return payload


def serialize_change_request(request: RegistryChangeRequest) -> dict[str, Any]:
    return {
        "id": str(request.id),
        "tool_id": str(request.tool_id),
        "kind": request.kind,
        "current": json.loads(request.current_json or "{}"),
        "requested": json.loads(request.requested_json or "{}"),
        "reason": request.reason,
        "status": request.status,
        "decision_note": request.decision_note or None,
        "requested_by_user_id": (
            str(request.requested_by_user_id) if request.requested_by_user_id else None
        ),
        "decided_by_user_id": (
            str(request.decided_by_user_id) if request.decided_by_user_id else None
        ),
        "self_decided": (
            request.decided_by_user_id is not None
            and request.decided_by_user_id == request.requested_by_user_id
        ),
        "decided_at": request.decided_at.isoformat() if request.decided_at else None,
        "created_at": request.created_at.isoformat(),
    }
