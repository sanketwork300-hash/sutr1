"""Centralized role-based authorization.

Single source of truth for org roles and what each role may do. Endpoints ask
for a permission via `require_permission(...)` (FastAPI dependency) or
`ensure_agent_can(...)` (for agent-facing endpoints where the caller may be an
API key). Org scoping itself remains the responsibility of queries — this
layer answers "may this member perform this action in their org".

Design note: a role→permission matrix (RBAC) rather than an OpenFGA-style
relationship graph. The resource graph today is flat (org → resources), so
relationship-based authorization would add machinery without adding power.
The permission vocabulary is the extension point: per-resource grants can be
layered underneath the same `can()` call later without touching endpoints.
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException
from sqlmodel import Session, select

from sutr.db import get_session
from sutr.dependencies import AgentAuth, get_current_org, get_current_user
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User

# Role hierarchy, most to least privileged.
ROLES = ("owner", "admin", "developer", "member", "viewer")

_ALL = frozenset(ROLES)

# Permission → roles that hold it.
PERMISSIONS: dict[str, frozenset[str]] = {
    # Rename/delete the org, manage billing.
    "org:manage": frozenset({"owner"}),
    # Invite/remove members, change roles.
    "org:members:manage": frozenset({"owner", "admin"}),
    # Org-wide settings (approval expiry, workspaces).
    "org:settings:write": frozenset({"owner", "admin"}),
    # Install/uninstall integrations, manage custom MCP/API definitions.
    "integrations:manage": frozenset({"owner", "admin", "developer"}),
    # Execute tools (playground, REST, MCP with a user token).
    "tools:execute": frozenset({"owner", "admin", "developer", "member"}),
    # Change per-tool execution modes (allow / require_approval / deny).
    "policies:write": frozenset({"owner", "admin"}),
    # Decide approval requests.
    "approvals:decide": frozenset({"owner", "admin"}),
    # Create/revoke API keys.
    "api_keys:manage": frozenset({"owner", "admin", "developer"}),
    # Read logs and approval history.
    "logs:read": _ALL,
    # Read the control-plane audit trail.
    "audit:read": frozenset({"owner", "admin"}),
}


def role_can(role: str, permission: str) -> bool:
    allowed = PERMISSIONS.get(permission)
    if allowed is None:
        raise ValueError(f"Unknown permission: {permission}")
    return role in allowed


def _forbidden(permission: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "error": "permission_denied",
            "message": f"Your role does not allow this action ({permission}).",
        },
    )


@dataclass
class OrgContext:
    user: User
    org: Org
    membership: OrgMembership

    @property
    def role(self) -> str:
        return self.membership.role


def get_org_context(
    current_user: User = Depends(get_current_user),
    current_org: Org = Depends(get_current_org),
    session: Session = Depends(get_session),
) -> OrgContext:
    membership = session.exec(
        select(OrgMembership)
        .where(OrgMembership.user_id == current_user.id)
        .where(OrgMembership.org_id == current_org.id)
    ).first()
    if membership is None:
        # get_current_org derives the org from a membership, so hitting this
        # means the membership vanished mid-request — deny, never assume.
        raise HTTPException(status_code=403, detail="Not a member of this organization")
    return OrgContext(user=current_user, org=current_org, membership=membership)


def require_permission(permission: str):
    """Dependency factory: resolve the caller's org context and enforce a permission."""
    if permission not in PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission}")

    def _check(ctx: OrgContext = Depends(get_org_context)) -> OrgContext:
        if not role_can(ctx.role, permission):
            raise _forbidden(permission)
        return ctx

    return _check


def ensure_agent_can(session: Session, auth: AgentAuth, permission: str) -> None:
    """Permission check for agent-facing endpoints (get_agent_auth callers).

    API keys keep their documented capabilities (install + call) unchanged —
    they carry no role, so only human-user contexts are role-checked.
    """
    if auth.user is None:
        return
    membership = session.exec(
        select(OrgMembership)
        .where(OrgMembership.user_id == auth.user.id)
        .where(OrgMembership.org_id == auth.org.id)
    ).first()
    if membership is None or not role_can(membership.role, permission):
        raise _forbidden(permission)
