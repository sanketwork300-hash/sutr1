"""S5: MCP management tools that change the org's integrations are role-gated.

`sutr__install_integration`, `sutr__uninstall_integration` and
`sutr__start_oauth_flow` go through `integrations:manage`, exactly like their
REST equivalents. A viewer can still browse and describe.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from sutr.dependencies import AgentAuth
from sutr.mcp import management_tools
from sutr.models.api_key import ApiKey
from sutr.models.integration import InstalledIntegration
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User


@pytest.fixture(name="mcp_env")
def mcp_env_fixture(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    org = Org(id=uuid.uuid4(), name="test")
    user = User(id=uuid.uuid4(), email="viewer@example.com", hashed_password="x")
    with Session(engine) as session:
        session.add(org)
        session.add(user)
        session.add(
            OrgMembership(
                user_id=user.id,
                org_id=org.id,
                role="viewer",
                created_at=datetime.now(timezone.utc),
            )
        )
        session.add(
            InstalledIntegration(
                org_id=org.id,
                integration_id="github",
                type="remote_mcp",
                url="https://mcp.github/mcp",
                auth_method="token",
                connected=True,
            )
        )
        session.commit()
        # The handlers open their own sessions; hand the test plain, detached
        # rows rather than instances that expire with this one.
        session.refresh(org)
        session.refresh(user)
        session.expunge_all()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("sutr.mcp.management_tools.engine", engine)
    monkeypatch.setattr("sutr.mcp.management_tools.notify_tools_changed", _noop)
    monkeypatch.setattr("sutr.mcp.management_tools.refresh_one", _noop)
    return engine, org, user


def _set_role(engine, user, org, role: str) -> None:
    with Session(engine) as session:
        membership = session.get(OrgMembership, (user.id, org.id))
        membership.role = role
        session.add(membership)
        session.commit()


def _installed(engine, org) -> list[str]:
    with Session(engine) as session:
        rows = session.exec(
            select(InstalledIntegration).where(InstalledIntegration.org_id == org.id)
        ).all()
        return [r.integration_id for r in rows]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool",
    ["sutr__uninstall_integration", "sutr__install_integration", "sutr__start_oauth_flow"],
)
async def test_viewer_cannot_change_integrations_over_mcp(mcp_env, tool):
    engine, org, user = mcp_env
    auth = AgentAuth(org=org, user=user, api_key=None)

    result = await management_tools.dispatch(
        tool,
        {"integration_id": "github", "auth_method": "token", "token": "ghp_x"},
        auth,
    )

    payload = json.loads(result[0].text)
    assert payload["error"] == "permission_denied"
    assert _installed(engine, org) == ["github"]


@pytest.mark.anyio
async def test_viewer_can_still_browse_over_mcp(mcp_env):
    engine, org, user = mcp_env
    auth = AgentAuth(org=org, user=user, api_key=None)

    result = await management_tools.dispatch("sutr__list_installed_integrations", {}, auth)

    assert "github" in result[0].text


@pytest.mark.anyio
async def test_developer_can_uninstall_over_mcp(mcp_env):
    engine, org, user = mcp_env
    _set_role(engine, user, org, "developer")
    auth = AgentAuth(org=org, user=user, api_key=None)

    result = await management_tools.dispatch(
        "sutr__uninstall_integration", {"integration_id": "github"}, auth
    )

    assert "uninstalled" in result[0].text
    assert _installed(engine, org) == []


@pytest.mark.anyio
async def test_api_key_acts_with_its_recorded_role_over_mcp(mcp_env):
    engine, org, user = mcp_env
    # A key whose role lacks integrations:manage is refused like a session would be.
    key = ApiKey(
        org_id=org.id, created_by_user_id=user.id, name="k", key_prefix="ap_x", key_hash="h"
    )
    key.role = "member"
    auth = AgentAuth(org=org, user=None, api_key=key)

    result = await management_tools.dispatch(
        "sutr__uninstall_integration", {"integration_id": "github"}, auth
    )

    assert json.loads(result[0].text)["error"] == "permission_denied"
    assert _installed(engine, org) == ["github"]
