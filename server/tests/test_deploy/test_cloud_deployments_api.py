"""Creating a cloud deployment through the API.

The rules under test are the ones that keep one member's cloud identity from
becoming everyone's: a cloud provider needs a connection, that connection must
belong to the caller, and the placement it was created with is snapshotted on
the row so a later stop or delete addresses the same target.
"""

import json
import uuid

import pytest
from sqlmodel import select

from sutr.deploy.base import ProviderStatus, ProviderTarget
from sutr.models.deployment import Deployment
from sutr.models.provider_connection import ProviderConnection
from sutr.models.user import User
from tests.test_deploy.test_deployments_api import PETSTORE, FakeProvider


class FakeCloudProvider(FakeProvider):
    """A provider that needs a connection and some placement, like the real ones."""

    id = "gcp"
    display_name = "Fake Cloud Run"
    connection_provider = "gcp"
    creates = "A fake service."

    def __init__(self):
        super().__init__()
        self.targets: list[ProviderTarget] = []

    @property
    def config_fields(self):
        from sutr.deploy.base import ConfigField

        return (
            ConfigField(key="project", label="Project", kind="target"),
            ConfigField(key="region", label="Region", kind="region", default="us-central1"),
        )

    async def available(self, target=None):
        if target is not None:
            self.targets.append(target)
        return True, None

    async def deploy(self, spec):
        self.targets.append(spec.target)
        self.deployed.append(spec)
        return {"url": "https://fake-run.app/mcp", "service": "fake", "console_url": "https://c"}


@pytest.fixture(name="cloud_provider")
def cloud_provider_fixture(monkeypatch):
    fake = FakeCloudProvider()

    def resolve(provider_id):
        return fake if provider_id == "gcp" else None

    monkeypatch.setattr("sutr.api.deployments.get_provider", resolve)
    monkeypatch.setattr("sutr.api.deployments.provider_enabled", lambda pid: (True, None))
    monkeypatch.setattr("sutr.services.deployments.get_provider", resolve)
    monkeypatch.setattr("sutr.deploy.credentials.get_provider", resolve)
    fake.status_result = ProviderStatus(state="running")
    return fake


def _connect(session, org, user, provider="gcp"):
    from sutr.connections.store import save_connection

    connection = save_connection(
        session,
        org_id=org.id,
        user_id=user.id,
        provider=provider,
        access_token_value="ya29.token",
        refresh_token_value="refresh",
        scopes="cloud-platform",
        account_label="dev@example.com",
        expires=None,
        metadata={},
    )
    session.commit()
    return connection


async def _project(client) -> str:
    resp = await client.post(
        "/api/openapi/import",
        json={"source_kind": "paste", "content": json.dumps(PETSTORE)},
    )
    return resp.json()["id"]


async def test_providers_expose_the_fields_the_builder_must_collect(client):
    """The builder renders a provider's form from this, so a new provider is a
    server change and nothing else."""
    resp = await client.get("/api/deployments/providers")
    assert resp.status_code == 200
    by_id = {entry["id"]: entry for entry in resp.json()}
    assert {"docker", "gcp", "azure", "aws"} <= set(by_id)
    assert by_id["docker"]["connection_provider"] is None
    assert by_id["aws"]["connection_provider"] == "aws"
    keys = [field["key"] for field in by_id["azure"]["config_fields"]]
    assert keys == ["subscription", "location", "resource_group", "registry", "environment"]
    assert all(field["help"] for field in by_id["aws"]["config_fields"])


async def test_a_cloud_deployment_requires_a_connection(client, cloud_provider):
    project_id = await _project(client)
    resp = await client.post(
        "/api/deployments",
        json={
            "project_id": project_id,
            "name": "Petstore",
            "provider": "gcp",
            "provider_config": {"project": "acme"},
        },
    )
    assert resp.status_code == 400
    assert "connected" in resp.json()["detail"]


async def test_required_placement_is_checked_before_anything_is_created(
    client, session, cloud_provider, test_org, test_user
):
    connection = _connect(session, test_org, test_user)
    project_id = await _project(client)
    resp = await client.post(
        "/api/deployments",
        json={
            "project_id": project_id,
            "name": "Petstore",
            "provider": "gcp",
            "connection_id": str(connection.id),
            "provider_config": {},
        },
    )
    assert resp.status_code == 400
    assert "Project" in resp.json()["detail"]
    assert session.exec(select(Deployment)).all() == []


async def test_placement_defaults_are_stored_not_left_implicit(
    client, session, cloud_provider, test_org, test_user
):
    connection = _connect(session, test_org, test_user)
    project_id = await _project(client)
    resp = await client.post(
        "/api/deployments",
        json={
            "project_id": project_id,
            "name": "Petstore",
            "provider": "gcp",
            "connection_id": str(connection.id),
            "provider_config": {"project": "acme-prod"},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["connection_id"] == str(connection.id)
    # `region` was never sent; the declared default is recorded anyway.
    assert body["config"] == {"project": "acme-prod", "region": "us-central1"}

    row = session.exec(select(Deployment)).one()
    assert json.loads(row.config_json)["region"] == "us-central1"


async def test_the_deploy_receives_the_connection_credentials(
    client, session, cloud_provider, test_org, test_user
):
    connection = _connect(session, test_org, test_user)
    project_id = await _project(client)
    resp = await client.post(
        "/api/deployments",
        json={
            "project_id": project_id,
            "name": "Petstore",
            "provider": "gcp",
            "connection_id": str(connection.id),
            "provider_config": {"project": "acme-prod"},
        },
    )
    assert resp.status_code == 201
    # available() runs with a real target before the row is written.
    assert cloud_provider.targets
    assert cloud_provider.targets[0].credentials["access_token"] == "ya29.token"
    assert cloud_provider.targets[0].config["project"] == "acme-prod"


async def test_another_members_connection_cannot_be_borrowed(
    client, session, cloud_provider, test_org
):
    other = User(email="other@example.com", hashed_password="x")
    session.add(other)
    session.commit()
    connection = ProviderConnection(
        org_id=test_org.id, user_id=other.id, provider="gcp", account_label="them@example.com"
    )
    session.add(connection)
    session.commit()

    project_id = await _project(client)
    resp = await client.post(
        "/api/deployments",
        json={
            "project_id": project_id,
            "name": "Petstore",
            "provider": "gcp",
            "connection_id": str(connection.id),
            "provider_config": {"project": "acme-prod"},
        },
    )
    assert resp.status_code == 404


async def test_a_connection_for_the_wrong_provider_is_rejected(
    client, session, cloud_provider, test_org, test_user
):
    connection = _connect(session, test_org, test_user, provider="github")
    project_id = await _project(client)
    resp = await client.post(
        "/api/deployments",
        json={
            "project_id": project_id,
            "name": "Petstore",
            "provider": "gcp",
            "connection_id": str(connection.id),
            "provider_config": {"project": "acme-prod"},
        },
    )
    assert resp.status_code == 404


async def test_a_disconnected_account_leaves_an_actionable_deployment(
    session, cloud_provider, test_org, test_user
):
    """Deleting the connection must not strand the row in a state whose error
    blames the cloud."""
    from sutr.deploy.credentials import resolve_target

    deployment = Deployment(
        org_id=test_org.id,
        name="Petstore",
        slug="petstore",
        provider="gcp",
        connection_id=uuid.uuid4(),
        config_json=json.dumps({"project": "acme-prod"}),
        package_zip=b"zip",
    )
    session.add(deployment)
    session.commit()

    with pytest.raises(Exception) as excinfo:
        await resolve_target(session, deployment)
    assert "disconnected" in str(excinfo.value)


async def test_listing_reuses_one_credential_per_placement(
    client, session, cloud_provider, test_org, test_user, monkeypatch
):
    """Reconciling a page of deployments must not mint a credential per row."""
    connection = _connect(session, test_org, test_user)
    project_id = await _project(client)
    for name in ("A", "B", "C"):
        resp = await client.post(
            "/api/deployments",
            json={
                "project_id": project_id,
                "name": name,
                "provider": "gcp",
                "connection_id": str(connection.id),
                "provider_config": {"project": "acme-prod"},
            },
        )
        assert resp.status_code == 201

    from sutr.deploy import credentials as credentials_module

    calls = {"n": 0}
    real = credentials_module.access_token

    async def counting(session_arg, connection_arg):
        calls["n"] += 1
        return await real(session_arg, connection_arg)

    monkeypatch.setattr(credentials_module, "access_token", counting)

    listed = await client.get("/api/deployments")
    assert listed.status_code == 200
    assert len(listed.json()) == 3
    # Three rows, one account, one placement — one credential.
    assert calls["n"] == 1
