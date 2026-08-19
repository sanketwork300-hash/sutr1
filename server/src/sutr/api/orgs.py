"""Organization lifecycle: info, rename, members, and invitations.

Roles come from sutr.authz. Safety invariants enforced here:
- an org always keeps at least one owner (no demote/remove of the last owner)
- invitations are single-use, expiring, and stored only as a token hash
- accepting an invitation never creates a new org (works on self-hosted too)
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from sutr.analytics import posthog_client
from sutr.auth_tokens import create_access_token
from sutr.authz import ROLES, OrgContext, get_org_context, require_permission
from sutr.config import settings
from sutr.db import get_session
from sutr.email import normalize_email
from sutr.email.send import send_email
from sutr.email.templates import org_invitation_email
from sutr.models.org import Org
from sutr.models.org_invitation import OrgInvitation
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User
from sutr.security import hash_password

router = APIRouter(prefix="/api/org", tags=["org"])

INVITATION_TTL_DAYS = 7

# Roles that can be granted through membership management. "owner" is included:
# an owner may promote another member to co-owner.
ASSIGNABLE_ROLES = set(ROLES)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class OrgResponse(BaseModel):
    id: str
    name: str
    role: str


class MemberResponse(BaseModel):
    user_id: str
    email: str
    role: str
    is_you: bool


class InvitationResponse(BaseModel):
    id: str
    email: str
    role: str
    created_at: datetime
    expires_at: datetime
    status: str  # pending | accepted | revoked | expired


class CreateInvitationRequest(BaseModel):
    email: str
    role: str = "member"


class CreateInvitationResponse(InvitationResponse):
    # The raw invite link is returned once so self-hosted installs without a
    # configured email backend can hand it to the invitee out-of-band.
    invite_url: str


class UpdateMemberRequest(BaseModel):
    role: str


class RenameOrgRequest(BaseModel):
    name: str


def _invitation_status(inv: OrgInvitation) -> str:
    if inv.revoked_at is not None:
        return "revoked"
    if inv.accepted_at is not None:
        return "accepted"
    if _aware(inv.expires_at) <= _utcnow():
        return "expired"
    return "pending"


def _serialize_invitation(inv: OrgInvitation) -> InvitationResponse:
    return InvitationResponse(
        id=str(inv.id),
        email=inv.email,
        role=inv.role,
        created_at=inv.created_at,
        expires_at=inv.expires_at,
        status=_invitation_status(inv),
    )


def _count_owners(session: Session, org_id: uuid.UUID) -> int:
    rows = session.exec(
        select(OrgMembership)
        .where(OrgMembership.org_id == org_id)
        .where(OrgMembership.role == "owner")
    ).all()
    return len(rows)


@router.get("", response_model=OrgResponse)
def get_org(ctx: OrgContext = Depends(get_org_context)) -> OrgResponse:
    return OrgResponse(id=str(ctx.org.id), name=ctx.org.name, role=ctx.role)


@router.patch("", response_model=OrgResponse)
def rename_org(
    body: RenameOrgRequest,
    ctx: OrgContext = Depends(require_permission("org:manage")),
    session: Session = Depends(get_session),
) -> OrgResponse:
    name = body.name.strip()
    if not name or len(name) > 120:
        raise HTTPException(status_code=400, detail="Organization name must be 1-120 characters")
    ctx.org.name = name
    session.add(ctx.org)
    session.commit()
    session.refresh(ctx.org)
    return OrgResponse(id=str(ctx.org.id), name=ctx.org.name, role=ctx.role)


@router.get("/members", response_model=list[MemberResponse])
def list_members(
    ctx: OrgContext = Depends(get_org_context),
    session: Session = Depends(get_session),
) -> list[MemberResponse]:
    rows = session.exec(
        select(OrgMembership, User)
        .where(OrgMembership.org_id == ctx.org.id)
        .where(OrgMembership.user_id == User.id)
    ).all()
    return [
        MemberResponse(
            user_id=str(user.id),
            email=user.email,
            role=membership.role,
            is_you=user.id == ctx.user.id,
        )
        for membership, user in rows
    ]


@router.patch("/members/{user_id}", response_model=MemberResponse)
def update_member_role(
    user_id: uuid.UUID,
    body: UpdateMemberRequest,
    ctx: OrgContext = Depends(require_permission("org:members:manage")),
    session: Session = Depends(get_session),
) -> MemberResponse:
    if body.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(ROLES)}")
    # Only owners may grant or revoke ownership.
    membership = session.get(OrgMembership, (user_id, ctx.org.id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if (body.role == "owner" or membership.role == "owner") and ctx.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can change ownership")
    if (
        membership.role == "owner"
        and body.role != "owner"
        and _count_owners(session, ctx.org.id) <= 1
    ):
        raise HTTPException(status_code=409, detail="An organization must keep at least one owner")

    membership.role = body.role
    session.add(membership)
    session.commit()

    target = session.get(User, user_id)
    return MemberResponse(
        user_id=str(user_id),
        email=target.email if target else "",
        role=membership.role,
        is_you=user_id == ctx.user.id,
    )


@router.delete("/members/{user_id}", status_code=204)
def remove_member(
    user_id: uuid.UUID,
    ctx: OrgContext = Depends(get_org_context),
    session: Session = Depends(get_session),
) -> None:
    # Members may remove themselves (leave); removing others needs the permission.
    if user_id != ctx.user.id:
        from sutr.authz import role_can

        if not role_can(ctx.role, "org:members:manage"):
            raise HTTPException(status_code=403, detail="permission_denied")

    membership = session.get(OrgMembership, (user_id, ctx.org.id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if membership.role == "owner" and _count_owners(session, ctx.org.id) <= 1:
        raise HTTPException(status_code=409, detail="An organization must keep at least one owner")
    if membership.role == "owner" and ctx.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can remove an owner")

    session.delete(membership)
    session.commit()


@router.get("/invitations", response_model=list[InvitationResponse])
def list_invitations(
    ctx: OrgContext = Depends(require_permission("org:members:manage")),
    session: Session = Depends(get_session),
) -> list[InvitationResponse]:
    rows = session.exec(select(OrgInvitation).where(OrgInvitation.org_id == ctx.org.id)).all()
    return [_serialize_invitation(inv) for inv in rows]


@router.post("/invitations", status_code=201, response_model=CreateInvitationResponse)
def create_invitation(
    body: CreateInvitationRequest,
    ctx: OrgContext = Depends(require_permission("org:members:manage")),
    session: Session = Depends(get_session),
) -> CreateInvitationResponse:
    if body.role not in ASSIGNABLE_ROLES:
        raise HTTPException(status_code=400, detail=f"Role must be one of: {', '.join(ROLES)}")
    if body.role == "owner" and ctx.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can invite another owner")

    email = normalize_email(body.email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    existing_user = session.exec(select(User).where(User.email == email)).first()
    if existing_user is not None:
        already = session.get(OrgMembership, (existing_user.id, ctx.org.id))
        if already is not None:
            raise HTTPException(status_code=409, detail="Already a member of this organization")

    pending = [
        inv
        for inv in session.exec(
            select(OrgInvitation)
            .where(OrgInvitation.org_id == ctx.org.id)
            .where(OrgInvitation.email == email)
        ).all()
        if _invitation_status(inv) == "pending"
    ]
    if pending:
        raise HTTPException(
            status_code=409, detail="An invitation for this email is already pending"
        )

    raw_token = secrets.token_urlsafe(32)
    inv = OrgInvitation(
        org_id=ctx.org.id,
        email=email,
        role=body.role,
        token_hash=_hash_token(raw_token),
        invited_by_user_id=ctx.user.id,
        expires_at=_utcnow() + timedelta(days=INVITATION_TTL_DAYS),
    )
    session.add(inv)
    session.commit()
    session.refresh(inv)

    invite_url = f"{settings.ui_base_url}/join?token={raw_token}"
    send_email(
        to=email,
        subject=f"You've been invited to {ctx.org.name} on Sutr",
        html=org_invitation_email(ctx.org.name, ctx.user.email, invite_url),
    )
    posthog_client.capture(
        distinct_id=str(ctx.user.id),
        event="org_member_invited",
        properties={"org_id": str(ctx.org.id), "role": body.role},
    )

    return CreateInvitationResponse(
        **_serialize_invitation(inv).model_dump(),
        invite_url=invite_url,
    )


@router.delete("/invitations/{invitation_id}", status_code=204)
def revoke_invitation(
    invitation_id: uuid.UUID,
    ctx: OrgContext = Depends(require_permission("org:members:manage")),
    session: Session = Depends(get_session),
) -> None:
    inv = session.get(OrgInvitation, invitation_id)
    if inv is None or inv.org_id != ctx.org.id:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if _invitation_status(inv) != "pending":
        raise HTTPException(status_code=409, detail="Invitation is no longer pending")
    inv.revoked_at = _utcnow()
    session.add(inv)
    session.commit()


# ── Invitation acceptance (public router: token is the credential) ───────────

accept_router = APIRouter(prefix="/api/org-invitations", tags=["org"])


class InvitationPreviewResponse(BaseModel):
    org_name: str
    email: str
    role: str
    # Whether an account with the invited email already exists (drives the UI:
    # login-and-accept vs create-account-and-accept).
    account_exists: bool


class AcceptInvitationRequest(BaseModel):
    token: str
    # Required only when creating a new account for the invited email.
    password: str | None = None


class AcceptInvitationResponse(BaseModel):
    org_id: str
    org_name: str
    role: str
    created_account: bool
    # Present only when a fresh account was created: lets the UI sign the new
    # user straight in. Existing accounts must log in with their own password.
    access_token: str | None = None


def _load_pending_invitation(session: Session, raw_token: str) -> OrgInvitation:
    inv = session.exec(
        select(OrgInvitation).where(OrgInvitation.token_hash == _hash_token(raw_token))
    ).first()
    if inv is None or _invitation_status(inv) != "pending":
        raise HTTPException(status_code=404, detail="Invitation not found or no longer valid")
    return inv


@accept_router.get("/preview", response_model=InvitationPreviewResponse)
def preview_invitation(
    token: str,
    session: Session = Depends(get_session),
) -> InvitationPreviewResponse:
    inv = _load_pending_invitation(session, token)
    org = session.get(Org, inv.org_id)
    existing = session.exec(select(User).where(User.email == inv.email)).first()
    return InvitationPreviewResponse(
        org_name=org.name if org else "",
        email=inv.email,
        role=inv.role,
        account_exists=existing is not None,
    )


@accept_router.post("/accept", response_model=AcceptInvitationResponse)
def accept_invitation(
    body: AcceptInvitationRequest,
    session: Session = Depends(get_session),
) -> AcceptInvitationResponse:
    inv = _load_pending_invitation(session, body.token)
    org = session.get(Org, inv.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization no longer exists")

    user = session.exec(select(User).where(User.email == inv.email)).first()
    created_account = False
    if user is None:
        if not body.password:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "password_required",
                    "message": "Set a password to create your account.",
                },
            )
        if len(body.password) < 6:
            raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
        # Possession of the emailed invite token proves control of the mailbox,
        # so the account starts verified. No org is created — the user joins
        # the inviting org (this also works on single-org self-hosted installs).
        user = User(
            email=inv.email,
            hashed_password=hash_password(body.password),
            email_verified=True,
        )
        session.add(user)
        session.flush()
        created_account = True

    if session.get(OrgMembership, (user.id, org.id)) is None:
        session.add(
            OrgMembership(
                user_id=user.id,
                org_id=org.id,
                role=inv.role,
                created_at=_utcnow(),
            )
        )

    inv.accepted_at = _utcnow()
    inv.accepted_by_user_id = user.id
    session.add(inv)
    session.commit()

    posthog_client.capture(
        distinct_id=str(user.id),
        event="org_invitation_accepted",
        properties={"org_id": str(org.id), "role": inv.role, "created_account": created_account},
    )

    access_token = None
    if created_account:
        access_token = create_access_token(str(user.id), token_version=user.token_version)

    return AcceptInvitationResponse(
        org_id=str(org.id),
        org_name=org.name,
        role=inv.role,
        created_account=created_account,
        access_token=access_token,
    )
