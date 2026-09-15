"""The /v1/generation surface, including what one tenant must not see."""

import io
import json
import uuid
import zipfile

from sqlmodel import select

from sutr.models.audit_event import AuditEvent
from sutr.models.org import Org
from sutr.models.runtime_artifact import RuntimeArtifact

from .conftest import import_petstore


async def _generate(client, project_id, **body):
    payload = {"ir_uri": f"sutr://openapi-projects/{project_id}", **body}
    response = await client.post("/v1/generation/runtimes", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def test_the_lld_interface_generates_an_artifact(client):
    project_id = await import_petstore(client)
    data = await _generate(
        client, project_id, runtime="python", compile={"filters": {"exclude_tags": ["admin"]}}
    )
    artifact = data["artifact"]
    assert artifact["status"] == "validated"
    assert artifact["deployable"] is True
    assert artifact["tool_count"] == 2
    assert artifact["runtime"] == "python"
    assert data["reused"] is False
    assert [stage["name"] for stage in data["stages"]][0] == "knowledge"


async def test_a_bare_project_id_is_accepted_for_ir_uri(client):
    project_id = await import_petstore(client)
    response = await client.post("/v1/generation/runtimes", json={"ir_uri": project_id})
    assert response.status_code == 201


async def test_project_id_is_accepted_as_an_alias(client):
    project_id = await import_petstore(client)
    response = await client.post("/v1/generation/runtimes", json={"project_id": project_id})
    assert response.status_code == 201


async def test_an_external_ir_uri_is_refused_rather_than_fetched(client):
    """An endpoint that dereferenced an arbitrary URI would be request forgery."""
    response = await client.post(
        "/v1/generation/runtimes", json={"ir_uri": "http://169.254.169.254/latest/meta-data/"}
    )
    assert response.status_code == 400
    assert "not fetched" in response.json()["error"]["message"]


async def test_a_runtime_with_no_template_is_refused_by_name(client):
    project_id = await import_petstore(client)
    response = await client.post(
        "/v1/generation/runtimes", json={"ir_uri": project_id, "runtime": "go"}
    )
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "runtime_not_supported"
    assert "NOT IMPLEMENTED" in body["message"]


async def test_the_artifact_is_listed_read_and_downloadable(client):
    project_id = await import_petstore(client)
    data = await _generate(client, project_id)
    artifact_id = data["artifact"]["id"]

    listed = await client.get("/v1/generation/runtimes")
    assert [row["id"] for row in listed.json()["data"]["artifacts"]] == [artifact_id]

    detail = await client.get(f"/v1/generation/runtimes/{artifact_id}")
    assert detail.status_code == 200
    body = detail.json()["data"]
    assert body["manifest"]["tool_count"] == body["tool_count"]
    assert body["verification"]["package_intact"] is True

    package = await client.get(f"/v1/generation/runtimes/{artifact_id}/package")
    assert package.headers["content-type"] == "application/zip"
    assert package.headers["x-sutr-package-sha256"] == body["package_sha256"]
    with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
        assert "server.py" in archive.namelist()
        assert "sutr_metrics.py" in archive.namelist()


async def test_the_sbom_is_served_as_cyclonedx(client):
    project_id = await import_petstore(client)
    data = await _generate(client, project_id)
    response = await client.get(f"/v1/generation/runtimes/{data['artifact']['id']}/sbom")
    assert response.headers["content-type"].startswith("application/vnd.cyclonedx+json")
    document = json.loads(response.content)
    assert document["bomFormat"] == "CycloneDX"


async def test_the_validation_report_names_what_was_not_run(client):
    project_id = await import_petstore(client)
    data = await _generate(client, project_id)
    response = await client.get(f"/v1/generation/runtimes/{data['artifact']['id']}/validation")
    report = response.json()["data"]
    assert report["validated"] is True
    assert set(report["blocked"]) == {"security_scan", "vulnerability_scan"}


async def test_capabilities_reports_what_this_install_can_check_and_sign(client):
    response = await client.get("/v1/generation/capabilities")
    data = response.json()["data"]
    runtimes = {entry["id"]: entry for entry in data["runtimes"]}
    assert runtimes["python"]["available"] is True
    assert runtimes["go"]["available"] is False
    assert runtimes["node"]["unavailable_reason"]
    assert data["signing"]["available"] is False
    assert data["validation"]["security_scan"]["available"] is False


async def test_generating_is_audited(client, session):
    project_id = await import_petstore(client)
    data = await _generate(client, project_id)
    session.expire_all()
    event = session.exec(
        select(AuditEvent).where(AuditEvent.action == "generation.runtime_generated")
    ).one()
    assert json.loads(event.metadata_json)["build_hash"] == data["artifact"]["build_hash"]


async def test_generation_requires_permission(client, session, test_user, test_org):
    from sutr.models.org_membership import OrgMembership

    project_id = await import_petstore(client)
    membership = session.exec(
        select(OrgMembership).where(OrgMembership.user_id == test_user.id)
    ).one()
    membership.role = "member"
    session.add(membership)
    session.commit()
    response = await client.post("/v1/generation/runtimes", json={"ir_uri": project_id})
    assert response.status_code == 403


# ── Cross-tenant negatives (build prompt §61) ───────────────────────────────


def _other_org(session, test_user) -> Org:
    org = Org(name="Other", slug="other-generation", owner_user_id=test_user.id)
    session.add(org)
    session.commit()
    return org


def _foreign_artifact(session, org_id) -> RuntimeArtifact:
    artifact = RuntimeArtifact(
        org_id=org_id,
        name="Theirs",
        slug="theirs",
        runtime="python",
        build_hash="f" * 64,
        package_zip=b"PK",
        package_sha256="0" * 64,
        status="validated",
    )
    session.add(artifact)
    session.commit()
    return artifact


async def test_another_tenants_artifact_is_not_listed(client, session, test_user):
    _foreign_artifact(session, _other_org(session, test_user).id)
    listed = await client.get("/v1/generation/runtimes")
    assert listed.json()["data"]["artifacts"] == []


async def test_another_tenants_artifact_reads_as_404_not_403(client, session, test_user):
    """403 would confirm the artifact exists, which is the thing being hidden."""
    artifact = _foreign_artifact(session, _other_org(session, test_user).id)
    for suffix in ("", "/manifest", "/sbom", "/validation", "/package"):
        response = await client.get(f"/v1/generation/runtimes/{artifact.id}{suffix}")
        assert response.status_code == 404, suffix


async def test_a_knowledge_uri_cannot_probe_for_another_tenants_project(client, session, test_user):
    project_id = await import_petstore(client)
    other = _other_org(session, test_user)
    from sutr.models.openapi_project import OpenAPIProject

    theirs = OpenAPIProject(
        org_id=other.id, name="Theirs", spec_text="{}", ir_json='{"title":"x","version":"1"}'
    )
    session.add(theirs)
    session.commit()
    response = await client.post(
        "/v1/generation/runtimes",
        json={"ir_uri": project_id, "knowledge_uri": str(theirs.id)},
    )
    assert response.status_code == 404


async def test_a_missing_project_is_404(client):
    response = await client.post("/v1/generation/runtimes", json={"ir_uri": str(uuid.uuid4())})
    assert response.status_code == 404
