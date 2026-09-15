"""Deployment API tests with a fake provider: full lifecycle, secrets, RBAC,
status reconciliation, and audit."""

import json
import uuid

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

    async def available(self, target=None):
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

    async def status(self, state, target=None):
        self.calls.append(("status", state))
        return self.status_result

    async def start(self, state, target=None):
        self.calls.append(("start", state))

    async def stop(self, state, target=None):
        self.calls.append(("stop", state))

    async def remove(self, state, target=None):
        self.calls.append(("remove", state))

    async def logs(self, state, target=None, tail=100):
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


# ── Update, rollback, history, metrics (build prompt §36/§37, ADR-016) ───────


async def test_creating_a_deployment_opens_revision_one(client, session, fake_provider):
    created = await _create(client)
    revisions = await client.get(f"/api/deployments/{created['id']}/revisions")
    assert revisions.status_code == 200
    history = revisions.json()
    assert len(history) == 1
    assert history[0]["revision"] == 1
    assert history[0]["origin"] == "create"
    assert history[0]["outcome"] == "active"
    assert len(history[0]["package_sha256"]) == 64


async def test_an_update_adds_a_revision_and_keeps_the_deployment(client, session, fake_provider):
    created = await _create(client)
    deployment_id = created["id"]

    resp = await client.post(f"/api/deployments/{deployment_id}/update", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pending_revision"] == 2
    # Same deployment: same id, same URL.
    assert resp.json()["id"] == deployment_id

    history = (await client.get(f"/api/deployments/{deployment_id}/revisions")).json()
    assert [entry["revision"] for entry in history] == [2, 1]
    assert history[0]["origin"] == "update"
    assert history[0]["outcome"] == "active"
    assert history[1]["outcome"] == "superseded", "the old revision is retained, not deleted"

    detail = (await client.get(f"/api/deployments/{deployment_id}")).json()
    assert detail["current_revision"] == 2
    assert detail["status"] == "running"


async def test_an_update_is_not_a_delete_and_recreate(client, session, fake_provider):
    """The provider must never be asked to remove anything during an update."""
    created = await _create(client)
    fake_provider.calls.clear()
    await client.post(f"/api/deployments/{created['id']}/update", json={})
    assert not [call for call in fake_provider.calls if call[0] == "remove"]


async def test_the_second_revision_carries_a_different_artifact_tag(client, session, fake_provider):
    created = await _create(client)
    await client.post(f"/api/deployments/{created['id']}/update", json={})
    assert [spec.revision for spec in fake_provider.deployed] == [1, 2]
    tags = {spec.artifact_tag for spec in fake_provider.deployed}
    assert len(tags) == 2, "each revision must build a distinct, retained artifact"


async def test_rollback_restores_the_previous_revisions_package(client, session, fake_provider):
    created = await _create(client)
    deployment_id = created["id"]
    original_package = fake_provider.deployed[0].package_zip

    await client.post(f"/api/deployments/{deployment_id}/update", json={})
    resp = await client.post(f"/api/deployments/{deployment_id}/rollback", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["restoring_revision"] == 1
    assert resp.json()["pending_revision"] == 3

    # The package that was re-run is the one that actually ran before —
    # byte for byte, not a rebuild.
    assert fake_provider.deployed[-1].package_zip == original_package

    history = (await client.get(f"/api/deployments/{deployment_id}/revisions")).json()
    assert history[0]["origin"] == "rollback"
    assert history[0]["restored_from_revision"] == 1


async def test_rollback_can_name_an_explicit_revision(client, session, fake_provider):
    created = await _create(client)
    deployment_id = created["id"]
    await client.post(f"/api/deployments/{deployment_id}/update", json={})
    resp = await client.post(f"/api/deployments/{deployment_id}/rollback", json={"revision": 1})
    assert resp.json()["restoring_revision"] == 1


async def test_rollback_refuses_an_unknown_revision(client, session, fake_provider):
    created = await _create(client)
    resp = await client.post(f"/api/deployments/{created['id']}/rollback", json={"revision": 99})
    assert resp.status_code == 404


async def test_rollback_refuses_when_there_is_nothing_earlier(client, session, fake_provider):
    created = await _create(client)
    resp = await client.post(f"/api/deployments/{created['id']}/rollback", json={})
    assert resp.status_code == 400
    assert "no earlier revision" in resp.json()["detail"]


async def test_update_and_rollback_are_audited(client, session, test_org, fake_provider):
    created = await _create(client)
    await client.post(f"/api/deployments/{created['id']}/update", json={})
    await client.post(f"/api/deployments/{created['id']}/rollback", json={})
    actions = {
        event.action
        for event in session.exec(
            select(AuditEvent).where(AuditEvent.target_id == created["id"])
        ).all()
    }
    assert {"deployment.update_requested", "deployment.rollback_requested"} <= actions
    assert {"deployment.updated", "deployment.rolled_back"} <= actions


async def test_metrics_are_reported_with_their_source(client, session, fake_provider):
    created = await _create(client)
    resp = await client.get(f"/api/deployments/{created['id']}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deployment_id"] == created["id"]
    assert body["revision"] == 1
    # The fake provider does not override metrics, so the default answer
    # names itself rather than reporting zeros.
    assert body["cpu_percent"] is None
    assert body["unavailable_reason"]


async def test_a_viewer_cannot_update_or_roll_back(
    client, session, test_user, test_org, fake_provider
):
    created = await _create(client)
    membership = session.exec(
        select(OrgMembership)
        .where(OrgMembership.user_id == test_user.id)
        .where(OrgMembership.org_id == test_org.id)
    ).one()
    membership.role = "viewer"
    session.add(membership)
    session.commit()

    assert (
        await client.post(f"/api/deployments/{created['id']}/update", json={})
    ).status_code == 403
    assert (
        await client.post(f"/api/deployments/{created['id']}/rollback", json={})
    ).status_code == 403
    assert (await client.get(f"/api/deployments/{created['id']}/revisions")).status_code == 403


# ── Idempotent creation (build prompt §76) ───────────────────────────────────


async def test_a_retry_with_the_same_key_returns_the_original_deployment(
    client, session, fake_provider
):
    """A deployment is slow, expensive and retry-prone; a duplicate means two
    containers and two bills for one intent."""
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    project_id = resp.json()["id"]
    payload = {"project_id": project_id, "name": "Petstore Runner", "provider": "docker"}

    first = await client.post(
        "/api/deployments", json=payload, headers={"Idempotency-Key": "deploy-once"}
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/deployments", json=payload, headers={"Idempotency-Key": "deploy-once"}
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"], "a retry created a second deployment"

    assert len(session.exec(select(Deployment)).all()) == 1


async def test_the_same_key_with_a_different_body_is_refused(client, session, fake_provider):
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    project_id = resp.json()["id"]

    first = await client.post(
        "/api/deployments",
        json={"project_id": project_id, "name": "One", "provider": "docker"},
        headers={"Idempotency-Key": "reused"},
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/deployments",
        json={"project_id": project_id, "name": "Two", "provider": "docker"},
        headers={"Idempotency-Key": "reused"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    assert "different request body" in second.json()["error"]["message"]


async def test_without_the_header_nothing_changes(client, session, fake_provider):
    """Idempotency is opt-in: an existing client that never sends the header
    must behave exactly as it did before."""
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    project_id = resp.json()["id"]
    payload = {"project_id": project_id, "name": "Petstore Runner", "provider": "docker"}

    a = await client.post("/api/deployments", json=payload)
    b = await client.post("/api/deployments", json=payload)
    assert a.status_code == 201
    assert b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
    assert len(session.exec(select(Deployment)).all()) == 2


async def test_a_failed_attempt_does_not_lock_the_key(client, session, fake_provider):
    """Otherwise a transient failure would block the retry for a day."""
    bad = await client.post(
        "/api/deployments",
        json={"project_id": str(uuid.uuid4()), "name": "Nope", "provider": "docker"},
        headers={"Idempotency-Key": "will-fail"},
    )
    assert bad.status_code == 404

    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    good = await client.post(
        "/api/deployments",
        json={
            "project_id": resp.json()["id"],
            "name": "Now works",
            "provider": "docker",
        },
        headers={"Idempotency-Key": "will-fail"},
    )
    assert good.status_code == 201


async def test_deploying_announces_a_runtime_event(client, session, fake_provider):
    from sutr.events import topics
    from sutr.models.outbox_event import OutboxEvent

    await _create(client)
    types = {row.event_type for row in session.exec(select(OutboxEvent)).all()}
    assert topics.RUNTIME_DEPLOYED in types


async def test_an_update_and_a_rollback_each_announce_themselves(client, session, fake_provider):
    from sutr.events import topics
    from sutr.models.outbox_event import OutboxEvent

    created = await _create(client)
    await client.post(f"/api/deployments/{created['id']}/update", json={})
    await client.post(f"/api/deployments/{created['id']}/rollback", json={})
    types = {row.event_type for row in session.exec(select(OutboxEvent)).all()}
    assert topics.RUNTIME_UPDATED in types
    assert topics.RUNTIME_ROLLED_BACK in types
