"""Deployment API tests with a fake provider: full lifecycle, secrets, RBAC,
status reconciliation, and audit."""

import json

import pytest
from sqlmodel import select

from sutr.deploy.base import DeploymentProvider, ProviderStatus
from sutr.models.audit_event import AuditEvent
from sutr.models.deployment import Deployment
from sutr.models.org_membership import OrgMembership
from sutr.models.secret import Secret
from tests.test_api.test_openapi_projects import PETSTORE


class FakeProvider(DeploymentProvider):
    id = "docker"
    display_name = "Fake Docker"

    def __init__(self):
        self.deployed = []
        self.calls = []
        self.status_result = ProviderStatus(state="running")

    async def available(self):
        return True, None

    async def deploy(self, spec):
        self.deployed.append(spec)
        return {
            "container_name": f"fake-{spec.slug}",
            "container_id": "fakecid",
            "image_tag": f"fake-{spec.slug}:1",
            "host_port": "59999",
            "url": "http://127.0.0.1:59999/mcp",
            "health_url": "http://127.0.0.1:59999/health",
        }

    async def status(self, state):
        self.calls.append(("status", state))
        return self.status_result

    async def start(self, state):
        self.calls.append(("start", state))

    async def stop(self, state):
        self.calls.append(("stop", state))

    async def remove(self, state):
        self.calls.append(("remove", state))

    async def logs(self, state, tail=100):
        self.calls.append(("logs", state, tail))
        return "fake log line\n"


@pytest.fixture(name="fake_provider")
def fake_provider_fixture(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr("sutr.api.deployments.get_provider", lambda pid: fake)
    monkeypatch.setattr("sutr.api.deployments.provider_enabled", lambda pid: (True, None))
    monkeypatch.setattr("sutr.services.deployments.get_provider", lambda pid: fake)
    return fake


async def _create(client, token="sk_upstream_secret", name="Petstore Runner") -> dict:
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    project_id = resp.json()["id"]
    resp = await client.post(
        "/api/deployments",
        json={
            "project_id": project_id,
            "name": name,
            "provider": "docker",
            "token": token,
            "compile": {"filters": {"exclude_tags": ["admin"]}},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_deploys_in_background_with_secret_env(
    client, session, test_org, fake_provider
):
    created = await _create(client)
    assert created["status"] == "queued"
    assert created["tool_count"] == 2
    assert created["env_var"] == "PETSTORE_RUNNER_API_TOKEN"
    assert created["has_token"] is True

    # The ASGI transport runs background tasks after the response — by now the
    # fake provider has deployed and the row is running.
    detail = await client.get(f"/api/deployments/{created['id']}")
    body = detail.json()
    assert body["status"] == "running"
    assert body["url"] == "http://127.0.0.1:59999/mcp"

    # The runtime token went through the secrets backend and into the spec env.
    spec = fake_provider.deployed[0]
    assert spec.env == {"PETSTORE_RUNNER_API_TOKEN": "sk_upstream_secret"}
    session.expire_all()
    assert session.exec(select(Secret).where(Secret.kind == "deployment_token")).one()

    actions = {
        e.action
        for e in session.exec(select(AuditEvent).where(AuditEvent.org_id == test_org.id)).all()
    }
    assert "deployment.created" in actions
    assert "deployment.started" in actions


async def test_create_rejected_when_provider_disabled(client, session, monkeypatch):
    monkeypatch.setattr(
        "sutr.api.deployments.provider_enabled", lambda pid: (False, "not on cloud")
    )
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    resp = await client.post(
        "/api/deployments",
        json={"project_id": resp.json()["id"], "name": "X", "provider": "docker"},
    )
    assert resp.status_code == 400
    assert "not on cloud" in resp.json()["detail"]


async def test_lifecycle_stop_start_logs_delete(client, session, test_org, fake_provider):
    created = await _create(client)
    deployment_id = created["id"]

    resp = await client.post(f"/api/deployments/{deployment_id}/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"
    assert (
        "stop",
        {
            "container_name": "fake-petstore_runner",
            "container_id": "fakecid",
            "image_tag": "fake-petstore_runner:1",
            "host_port": "59999",
            "url": "http://127.0.0.1:59999/mcp",
            "health_url": "http://127.0.0.1:59999/health",
        },
    ) in fake_provider.calls

    resp = await client.post(f"/api/deployments/{deployment_id}/start")
    assert resp.json()["status"] == "running"

    resp = await client.get(f"/api/deployments/{deployment_id}/logs?tail=50")
    assert resp.json()["logs"] == "fake log line\n"

    resp = await client.delete(f"/api/deployments/{deployment_id}")
    assert resp.status_code == 204
    assert any(c[0] == "remove" for c in fake_provider.calls)

    session.expire_all()
    assert session.exec(select(Deployment)).first() is None
    assert session.exec(select(Secret).where(Secret.kind == "deployment_token")).first() is None
    actions = {
        e.action
        for e in session.exec(select(AuditEvent).where(AuditEvent.org_id == test_org.id)).all()
    }
    assert {"deployment.stopped", "deployment.deleted"} <= actions


async def test_status_reconciles_with_provider(client, session, fake_provider):
    created = await _create(client)
    fake_provider.status_result = ProviderStatus(state="not_found", detail="container gone")

    detail = await client.get(f"/api/deployments/{created['id']}")
    assert detail.json()["status"] == "failed"
    assert "container gone" in detail.json()["error"]


async def test_viewer_cannot_manage_deployments(
    client, session, test_user, test_org, fake_provider
):
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    project_id = resp.json()["id"]

    membership = session.get(OrgMembership, (test_user.id, test_org.id))
    membership.role = "viewer"
    session.add(membership)
    session.commit()

    resp = await client.post(
        "/api/deployments", json={"project_id": project_id, "name": "X", "provider": "docker"}
    )
    assert resp.status_code == 403
