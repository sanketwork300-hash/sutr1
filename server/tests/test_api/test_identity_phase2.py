"""Phase 2 identity tests: logout, token versioning, TOTP as a login factor,
maintenance sweeps, deterministic org resolution."""

import time
import uuid
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from fastapi import HTTPException
from sqlmodel import select

from agent_port import dependencies as deps
from agent_port.auth_tokens import create_access_token
from agent_port.maintenance import run_maintenance_sweep
from agent_port.models.oauth_revoked_token import OAuthRevokedToken
from agent_port.models.org import Org
from agent_port.models.org_membership import OrgMembership
from agent_port.models.tool_approval_request import ToolApprovalRequest
from agent_port.models.user import User
from agent_port.security import hash_password
from agent_port.totp import generate_secret


async def test_logout_revokes_token(client, session, test_user):
    token = create_access_token(str(test_user.id), token_version=test_user.token_version)
    resp = await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204
    assert len(session.exec(select(OAuthRevokedToken)).all()) == 1

    # The revoked token no longer authenticates via the real dependency.
    with pytest.raises(HTTPException) as exc:
        deps.get_current_user(token=token, session=session)
    assert exc.value.status_code == 401

    # Idempotent.
    resp = await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204
    assert len(session.exec(select(OAuthRevokedToken)).all()) == 1


def test_token_version_invalidates_old_tokens(session, test_user):
    old_token = create_access_token(str(test_user.id), token_version=test_user.token_version)
    assert deps.get_current_user(token=old_token, session=session).id == test_user.id

    test_user.token_version = (test_user.token_version or 0) + 1
    session.add(test_user)
    session.commit()

    with pytest.raises(HTTPException):
        deps.get_current_user(token=old_token, session=session)

    new_token = create_access_token(str(test_user.id), token_version=test_user.token_version)
    assert deps.get_current_user(token=new_token, session=session).id == test_user.id


async def test_password_change_bumps_token_version(client, session, test_user):
    test_user.hashed_password = hash_password("oldpass123")
    session.add(test_user)
    session.commit()
    before = test_user.token_version or 0

    resp = await client.post(
        "/api/users/me/change-password",
        json={"current_password": "oldpass123", "new_password": "newpass123"},
    )
    assert resp.status_code == 200
    session.refresh(test_user)
    assert test_user.token_version == before + 1


async def test_login_requires_totp_when_enabled(client, session, test_user):
    secret = generate_secret()
    test_user.hashed_password = hash_password("correct-horse")
    test_user.email_verified = True
    test_user.totp_secret = secret
    test_user.totp_enabled = True
    session.add(test_user)
    session.commit()

    form = {"username": "test@example.com", "password": "correct-horse"}
    resp = await client.post("/api/auth/token", data=form)
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "totp_required"

    resp = await client.post("/api/auth/token", data={**form, "totp_code": "000000"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "totp_invalid"

    code = pyotp.TOTP(secret).now()
    resp = await client.post("/api/auth/token", data={**form, "totp_code": code})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


async def test_login_without_totp_unchanged(client, session, test_user):
    test_user.hashed_password = hash_password("correct-horse")
    test_user.email_verified = True
    session.add(test_user)
    session.commit()
    resp = await client.post(
        "/api/auth/token", data={"username": "test@example.com", "password": "correct-horse"}
    )
    assert resp.status_code == 200


def test_maintenance_sweep(session, test_org):
    session.add(OAuthRevokedToken(token_hash="deadbeef", expires_at=int(time.time()) - 100))
    session.add(OAuthRevokedToken(token_hash="livebeef", expires_at=int(time.time()) + 10_000))
    session.add(
        ToolApprovalRequest(
            org_id=test_org.id,
            integration_id="github",
            tool_name="x",
            args_json="{}",
            args_hash="h",
            summary_text="s",
            status="pending",
            requested_at=datetime.utcnow() - timedelta(hours=2),
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
    )
    session.commit()
    counts = run_maintenance_sweep()
    session.expire_all()
    assert counts["revoked_tokens_pruned"] == 1
    assert counts["approvals_expired"] == 1
    remaining = session.exec(select(OAuthRevokedToken)).all()
    assert [r.token_hash for r in remaining] == ["livebeef"]


def test_default_membership_is_deterministic(session, test_user, test_org):
    org2 = Org(id=uuid.uuid4(), name="Second Org")
    session.add(org2)
    session.commit()
    session.add(
        OrgMembership(
            user_id=test_user.id,
            org_id=org2.id,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    # test_org membership has NULL created_at (older) → wins deterministically.
    m = deps.default_membership(session, test_user.id)
    assert m.org_id == test_org.id


def test_resolve_org_honours_requested_org(session, test_user, test_org):
    org2 = Org(id=uuid.uuid4(), name="Second Org")
    session.add(org2)
    session.commit()
    session.add(
        OrgMembership(
            user_id=test_user.id,
            org_id=org2.id,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    assert deps._resolve_org(test_user, session, org2.id).id == org2.id
    assert deps._resolve_org(test_user, session, None).id == test_org.id
    with pytest.raises(HTTPException):
        deps._resolve_org(test_user, session, uuid.uuid4())


async def test_register_sets_membership_timestamp(client, session):
    resp = await client.post(
        "/api/users/register", json={"email": "stamped@example.com", "password": "abcdef"}
    )
    assert resp.status_code == 201
    user = session.exec(select(User).where(User.email == "stamped@example.com")).first()
    membership = session.exec(select(OrgMembership).where(OrgMembership.user_id == user.id)).first()
    assert membership.created_at is not None
