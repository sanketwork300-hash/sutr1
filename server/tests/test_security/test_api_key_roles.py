"""S2: API keys go through the role matrix with the role of the member who
minted them, instead of bypassing `ensure_agent_can` entirely."""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from sutr.authz import ensure_agent_can
from sutr.config import settings
from sutr.db import get_session
from sutr.dependencies import AgentAuth
from sutr.main import app
from sutr.models.api_key import ApiKey
from sutr.models.org_membership import OrgMembership


def _key(org, user, role: str) -> ApiKey:
    return ApiKey(
        org_id=org.id,
        created_by_user_id=user.id,
        name="k",
        key_prefix="ap_x",
        key_hash="h",
        role=role,
    )


@pytest.mark.parametrize(
    "permission", ["org:manage", "approvals:decide", "policies:write", "org:members:manage"]
)
def test_developer_key_is_denied_owner_and_admin_permissions(
    session, test_user, test_org, permission
):
    auth = AgentAuth(org=test_org, user=None, api_key=_key(test_org, test_user, "developer"))
    with pytest.raises(HTTPException) as exc:
        ensure_agent_can(session, auth, permission)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("permission", ["tools:execute", "integrations:manage", "api_keys:manage"])
def test_developer_key_keeps_documented_capabilities(session, test_user, test_org, permission):
    auth = AgentAuth(org=test_org, user=None, api_key=_key(test_org, test_user, "developer"))
    ensure_agent_can(session, auth, permission)  # does not raise


def test_owner_key_may_manage_the_org(session, test_user, test_org):
    auth = AgentAuth(org=test_org, user=None, api_key=_key(test_org, test_user, "owner"))
    ensure_agent_can(session, auth, "org:manage")


def test_principal_without_user_or_key_is_denied(session, test_org):
    auth = AgentAuth(org=test_org, user=None, api_key=None)
    with pytest.raises(HTTPException):
        ensure_agent_can(session, auth, "tools:execute")


@pytest.mark.anyio
async def test_created_key_records_the_creators_role(client, session, test_user, test_org):
    resp = await client.post("/api/api-keys", json={"name": "owner-key"})
    assert resp.status_code == 201
    assert resp.json()["role"] == "owner"

    membership = session.get(OrgMembership, (test_user.id, test_org.id))
    membership.role = "developer"
    session.add(membership)
    session.commit()

    resp = await client.post("/api/api-keys", json={"name": "dev-key"})
    assert resp.status_code == 201
    assert resp.json()["role"] == "developer"

    listed = {k["name"]: k["role"] for k in (await client.get("/api/api-keys")).json()}
    assert listed == {"owner-key": "owner", "dev-key": "developer"}


async def _with_key(session, plain_key: str, method: str, path: str, **kwargs):
    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test", headers={"X-API-Key": plain_key}
        ) as c:
            return await c.request(method, path, **kwargs)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_developer_key_cannot_reach_org_manage_endpoint(session, api_key_record):
    record, plain_key = api_key_record
    assert record.role == "developer"

    resp = await _with_key(
        session, plain_key, "POST", "/v1/billing/plans", json={"key": "k", "model": "flat"}
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "permission_denied"


@pytest.mark.anyio
async def test_owner_key_passes_the_org_manage_gate(session, api_key_record):
    record, plain_key = api_key_record
    record.role = "owner"
    session.add(record)
    session.commit()

    resp = await _with_key(
        session, plain_key, "POST", "/v1/billing/plans", json={"key": "k", "model": "flat"}
    )

    assert resp.status_code != 403


@pytest.mark.anyio
async def test_malformed_subject_claim_is_a_401_not_a_500(session):
    token = jwt.encode(
        {
            "sub": "not-a-uuid",
            "token_use": "access",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            "tv": 0,
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as c:
            via_user = await c.get("/api/users/me")
            via_agent = await c.get("/api/integrations/custom-api")
    finally:
        app.dependency_overrides.clear()

    assert via_user.status_code == 401
    assert via_agent.status_code == 401


def test_api_key_default_role_matches_migration_default():
    assert (
        ApiKey(
            org_id=uuid.uuid4(),
            created_by_user_id=uuid.uuid4(),
            name="k",
            key_prefix="p",
            key_hash="h",
        ).role
        == "developer"
    )
