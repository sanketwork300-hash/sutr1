"""LLD §3.6: only validated artifacts are deployable.

The gate is only worth anything where it is enforced, so these tests go through
the deployment API rather than calling `artifacts.deployable` directly.
"""

import json
import uuid

from sqlmodel import select

from sutr.models.deployment import Deployment
from sutr.models.deployment_revision import DeploymentRevision
from sutr.models.org import Org
from sutr.models.runtime_artifact import STATUS_REJECTED, RuntimeArtifact

# The fake provider from the deployment tests: these tests are about the gate,
# not about reaching a container runtime.
from tests.test_deploy.test_deployments_api import fake_provider_fixture  # noqa: F401

from .conftest import import_petstore


async def _artifact(client, **body) -> dict:
    project_id = await import_petstore(client)
    response = await client.post("/v1/generation/runtimes", json={"ir_uri": project_id, **body})
    assert response.status_code == 201, response.text
    return response.json()["data"]["artifact"]


async def test_a_validated_artifact_can_be_deployed_and_is_recorded(client, session, fake_provider):
    artifact = await _artifact(client)
    response = await client.post(
        "/api/deployments",
        # A different name from the one the artifact was built under, so the
        # assertion below is about the artifact rather than a coincidence.
        json={"artifact_id": artifact["id"], "name": "Renamed Runner", "provider": "docker"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["artifact_id"] == artifact["id"]

    deployment = session.exec(select(Deployment)).one()
    assert str(deployment.artifact_id) == artifact["id"]
    # The bytes deployed are the bytes that passed the gate.
    stored = session.get(RuntimeArtifact, uuid.UUID(artifact["id"]))
    assert deployment.package_zip == stored.package_zip
    # And the credential goes into the variable the package actually reads.
    assert deployment.env_var == "PETSTORE_API_TOKEN"


async def test_a_rejected_artifact_is_refused(client, session, fake_provider):
    artifact = await _artifact(client)
    row = session.get(RuntimeArtifact, uuid.UUID(artifact["id"]))
    row.status = STATUS_REJECTED
    row.validation_json = json.dumps({"validated": False, "failed": ["security_scan"]})
    session.add(row)
    session.commit()

    response = await client.post(
        "/api/deployments",
        json={"artifact_id": artifact["id"], "name": "Petstore", "provider": "docker"},
    )
    assert response.status_code == 409
    assert "security_scan" in response.json()["detail"]
    assert session.exec(select(Deployment)).all() == []


async def test_another_tenants_artifact_cannot_be_deployed(
    client, session, test_user, fake_provider
):
    other = Org(name="Other", slug="other-deploy", owner_user_id=test_user.id)
    session.add(other)
    session.commit()
    theirs = RuntimeArtifact(
        org_id=other.id,
        name="Theirs",
        slug="theirs",
        runtime="python",
        build_hash="f" * 64,
        package_zip=b"PK",
        package_sha256="0" * 64,
        status="validated",
    )
    session.add(theirs)
    session.commit()

    response = await client.post(
        "/api/deployments",
        json={"artifact_id": str(theirs.id), "name": "Theirs", "provider": "docker"},
    )
    assert response.status_code == 404


async def test_a_deployment_needs_exactly_one_source(client, fake_provider):
    response = await client.post(
        "/api/deployments", json={"name": "Petstore", "provider": "docker"}
    )
    assert response.status_code == 400
    assert "project_id" in response.json()["detail"]


async def test_updating_to_an_artifact_records_it_on_the_revision(client, session, fake_provider):
    artifact = await _artifact(client)
    created = await client.post(
        "/api/deployments",
        json={"artifact_id": artifact["id"], "name": "Petstore", "provider": "docker"},
    )
    deployment_id = created.json()["id"]

    second = await _artifact(client, compile={"filters": {"exclude_tags": ["admin"]}})
    assert second["id"] != artifact["id"]

    response = await client.post(
        f"/api/deployments/{deployment_id}/update", json={"artifact_id": second["id"]}
    )
    assert response.status_code == 200, response.text
    revision = response.json()["pending_revision"]

    session.expire_all()
    entry = session.exec(
        select(DeploymentRevision).where(DeploymentRevision.revision == revision)
    ).one()
    assert str(entry.artifact_id) == second["id"]


async def test_updating_to_a_rejected_artifact_is_refused(client, session, fake_provider):
    artifact = await _artifact(client)
    created = await client.post(
        "/api/deployments",
        json={"artifact_id": artifact["id"], "name": "Petstore", "provider": "docker"},
    )
    deployment_id = created.json()["id"]

    row = session.get(RuntimeArtifact, uuid.UUID(artifact["id"]))
    row.status = STATUS_REJECTED
    session.add(row)
    session.commit()

    response = await client.post(
        f"/api/deployments/{deployment_id}/update", json={"artifact_id": artifact["id"]}
    )
    assert response.status_code == 409
