"""Phase 2 tests: org lifecycle, members, invitations, workspaces, RBAC."""

import uuid

import pytest
from sqlmodel import select

from sutr.authz import PERMISSIONS, ROLES, role_can
from sutr.models.org_invitation import OrgInvitation
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User
from sutr.models.workspace import Workspace


def _set_role(session, test_user, test_org, role: str) -> None:
    membership = session.get(OrgMembership, (test_user.id, test_org.id))
    membership.role = role
    session.add(membership)
    session.commit()


def test_role_matrix_is_consistent():
    for perm, roles in PERMISSIONS.items():
        assert roles <= set(ROLES), perm
    # Owner can do everything.
    for perm in PERMISSIONS:
        assert role_can("owner", perm)
    # Viewer can only read.
    assert role_can("viewer", "logs:read")
    assert not role_can("viewer", "tools:execute")
    assert not role_can("member", "approvals:decide")
    assert not role_can("developer", "policies:write")
    with pytest.raises(ValueError):
        role_can("owner", "nonexistent:permission")


async def test_get_org_returns_role(client):
    resp = await client.get("/api/org")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Test Org"
    assert body["role"] == "owner"


async def test_rename_org_owner_only(client, session, test_user, test_org):
    resp = await client.patch("/api/org", json={"name": "Renamed Org"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Org"

    _set_role(session, test_user, test_org, "admin")
    resp = await client.patch("/api/org", json={"name": "Nope"})
    assert resp.status_code == 403


async def test_member_list_and_role_change(client, session, test_user, test_org):
    other = User(id=uuid.uuid4(), email="other@example.com", hashed_password="x")
    session.add(other)
    session.commit()
    session.add(OrgMembership(user_id=other.id, org_id=test_org.id, role="member"))
    session.commit()

    resp = await client.get("/api/org/members")
    assert resp.status_code == 200
    members = {m["email"]: m for m in resp.json()}
    assert members["test@example.com"]["role"] == "owner"
    assert members["other@example.com"]["role"] == "member"

    resp = await client.patch(f"/api/org/members/{other.id}", json={"role": "developer"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "developer"

    resp = await client.patch(f"/api/org/members/{other.id}", json={"role": "bogus"})
    assert resp.status_code == 400


async def test_last_owner_cannot_be_demoted_or_removed(client, test_user):
    resp = await client.patch(f"/api/org/members/{test_user.id}", json={"role": "member"})
    assert resp.status_code == 409
    resp = await client.delete(f"/api/org/members/{test_user.id}")
    assert resp.status_code == 409


async def test_invite_accept_signup_flow(client, session, test_org):
    resp = await client.post(
        "/api/org/invitations", json={"email": "Newbie@Example.com", "role": "developer"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "newbie@example.com"
    assert body["status"] == "pending"
    assert "/join?token=" in body["invite_url"]
    token = body["invite_url"].split("token=")[1]

    # Duplicate pending invite is rejected.
    resp = await client.post("/api/org/invitations", json={"email": "newbie@example.com"})
    assert resp.status_code == 409

    resp = await client.get(f"/api/org-invitations/preview?token={token}")
    assert resp.status_code == 200
    assert resp.json() == {
        "org_name": "Test Org",
        "email": "newbie@example.com",
        "role": "developer",
        "account_exists": False,
    }

    # Accept without password → account creation requires one.
    resp = await client.post("/api/org-invitations/accept", json={"token": token})
    assert resp.status_code == 400

    resp = await client.post(
        "/api/org-invitations/accept", json={"token": token, "password": "hunter22"}
    )
    assert resp.status_code == 200
    assert resp.json()["created_account"] is True
    assert resp.json()["org_id"] == str(test_org.id)

    new_user = session.exec(select(User).where(User.email == "newbie@example.com")).first()
    assert new_user is not None
    assert new_user.email_verified is True
    membership = session.get(OrgMembership, (new_user.id, test_org.id))
    assert membership is not None and membership.role == "developer"

    # Token is single-use.
    resp = await client.post(
        "/api/org-invitations/accept", json={"token": token, "password": "hunter22"}
    )
    assert resp.status_code == 404


async def test_invite_existing_user_joins_without_new_org(client, session, test_org):
    existing = User(id=uuid.uuid4(), email="veteran@example.com", hashed_password="x")
    session.add(existing)
    session.commit()

    resp = await client.post("/api/org/invitations", json={"email": "veteran@example.com"})
    assert resp.status_code == 201
    token = resp.json()["invite_url"].split("token=")[1]

    resp = await client.post("/api/org-invitations/accept", json={"token": token})
    assert resp.status_code == 200
    assert resp.json()["created_account"] is False
    assert session.get(OrgMembership, (existing.id, test_org.id)) is not None


async def test_invitation_revoke(client):
    resp = await client.post("/api/org/invitations", json={"email": "gone@example.com"})
    inv_id = resp.json()["id"]
    resp = await client.delete(f"/api/org/invitations/{inv_id}")
    assert resp.status_code == 204
    resp = await client.get("/api/org/invitations")
    assert resp.json()[0]["status"] == "revoked"


async def test_member_cannot_manage_members(client, session, test_user, test_org):
    _set_role(session, test_user, test_org, "member")
    resp = await client.post("/api/org/invitations", json={"email": "x@example.com"})
    assert resp.status_code == 403
    # But can still read the member list.
    resp = await client.get("/api/org/members")
    assert resp.status_code == 200


async def test_workspaces_default_and_crud(client, session, test_org):
    resp = await client.get("/api/workspaces")
    assert resp.status_code == 200
    workspaces = resp.json()
    assert workspaces[0]["is_default"] is True
    assert workspaces[0]["slug"] == "default"
    default_id = workspaces[0]["id"]

    resp = await client.post("/api/workspaces", json={"name": "Production APIs"})
    assert resp.status_code == 201
    ws = resp.json()
    assert ws["slug"] == "production-apis"

    resp = await client.post("/api/workspaces", json={"name": "Production APIs"})
    assert resp.status_code == 409

    resp = await client.patch(f"/api/workspaces/{ws['id']}", json={"name": "Staging"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Staging"

    resp = await client.delete(f"/api/workspaces/{default_id}")
    assert resp.status_code == 409

    resp = await client.delete(f"/api/workspaces/{ws['id']}")
    assert resp.status_code == 204
    assert (
        session.exec(select(Workspace).where(Workspace.id == uuid.UUID(ws["id"]))).first() is None
    )


async def test_viewer_denied_policy_and_key_management(client, session, test_user, test_org):
    _set_role(session, test_user, test_org, "viewer")
    resp = await client.put(
        "/api/tool-settings/github/some_tool", json={"mode": "require_approval"}
    )
    assert resp.status_code == 403
    resp = await client.post("/api/api-keys", json={"name": "nope"})
    assert resp.status_code == 403
    resp = await client.patch("/api/org-settings", json={"approval_expiry_minutes": 30})
    assert resp.status_code == 403


async def test_admin_can_write_policies(client, session, test_user, test_org):
    _set_role(session, test_user, test_org, "admin")
    resp = await client.put(
        "/api/tool-settings/github/some_tool", json={"mode": "require_approval"}
    )
    assert resp.status_code == 200


async def test_invitations_survive_in_db(client, session, test_org):
    await client.post("/api/org/invitations", json={"email": "persist@example.com"})
    inv = session.exec(select(OrgInvitation).where(OrgInvitation.org_id == test_org.id)).first()
    assert inv is not None
    # Only the hash is stored — never the raw token.
    assert len(inv.token_hash) == 64
