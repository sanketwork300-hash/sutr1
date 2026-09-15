"""The pipeline: determinism, the event chain, and what a rejection does."""

import json
import uuid

import pytest
from sqlmodel import col, select

from sutr.events import topics
from sutr.generation import artifacts, pipeline
from sutr.models.outbox_event import OutboxEvent
from sutr.models.runtime_artifact import STATUS_REJECTED, STATUS_VALIDATED, RuntimeArtifact
from sutr.openapi import packaging

from .conftest import TOOLS


async def _generate(session, org_id, **overrides):
    kwargs = {
        "org_id": org_id,
        "project_id": uuid.uuid4(),
        "name": "Petstore Kit",
        "base_url": "https://api.petstore.example.com/v1",
        "token_header": "X-Api-Key",
        "token_format": "{token}",
        "tools": TOOLS,
        "api_title": "Petstore",
        "api_version": "1.2.0",
        "ir_version": 2,
        "ir_hash": "a" * 64,
    }
    kwargs.update(overrides)
    return await pipeline.generate(session, **kwargs)


async def test_a_generation_produces_a_validated_signed_artifact(session, test_org):
    outcome = await _generate(session, test_org.id)
    session.commit()

    artifact = outcome.artifact
    assert artifact.status == STATUS_VALIDATED
    assert artifact.tool_count == 2
    assert artifact.template_version == packaging.TEMPLATE_VERSION
    assert artifact.package_sha256 == artifacts.package_digest(artifact.package_zip)
    assert [stage.name for stage in outcome.stages] == [
        "knowledge",
        "generate",
        "validate",
        "store",
    ]
    # No signing key is configured in tests, and the record says so rather than
    # carrying an empty-looking signature.
    assert artifact.signature == ""
    assert artifacts.serialize(artifact)["signature"]["signed"] is False


async def test_generating_twice_from_unchanged_inputs_returns_the_same_artifact(session, test_org):
    """The determinism requirement, enforced rather than asserted.

    A non-deterministic packager would produce different bytes, a different
    build hash, and a second row — which is exactly what this asserts against.
    """
    project_id = uuid.uuid4()
    first = await _generate(session, test_org.id, project_id=project_id)
    session.commit()
    second = await _generate(session, test_org.id, project_id=project_id)
    session.commit()

    assert second.reused is True
    assert second.artifact.id == first.artifact.id
    assert second.artifact.build_hash == first.artifact.build_hash
    rows = session.exec(select(RuntimeArtifact)).all()
    assert len(rows) == 1
    # And it did not re-run the gate on bytes that already passed.
    assert next(s for s in second.stages if s.name == "validate").status == "reused"


async def test_a_different_specification_produces_a_different_artifact(session, test_org):
    project_id = uuid.uuid4()
    first = await _generate(session, test_org.id, project_id=project_id)
    session.commit()
    second = await _generate(
        session, test_org.id, project_id=project_id, tools=TOOLS[:1], ir_hash="b" * 64
    )
    session.commit()
    assert second.reused is False
    assert second.artifact.build_hash != first.artifact.build_hash
    assert len(session.exec(select(RuntimeArtifact)).all()) == 2


async def test_the_same_build_in_two_tenants_is_two_artifacts(session, test_org, test_user):
    from sutr.models.org import Org

    other = Org(name="Other", slug="other-gen", owner_user_id=test_user.id)
    session.add(other)
    session.flush()
    project_id = uuid.uuid4()
    first = await _generate(session, test_org.id, project_id=project_id)
    second = await _generate(session, other.id, project_id=project_id)
    session.commit()
    assert first.artifact.id != second.artifact.id
    assert first.artifact.build_hash == second.artifact.build_hash


async def test_the_lld_event_chain_is_published_in_order(session, test_org):
    outcome = await _generate(session, test_org.id)
    session.commit()

    events = session.exec(
        select(OutboxEvent)
        .where(OutboxEvent.resource_id == str(outcome.generation_id))
        .order_by(col(OutboxEvent.id))
    ).all()
    assert [event.event_type for event in events] == [
        topics.METADATA_GENERATED,
        topics.GENERATION_STARTED,
        topics.MCP_GENERATED,
        topics.VALIDATION_COMPLETED,
    ]
    generated = json.loads(events[2].envelope_json)["payload"]
    assert generated["artifact_id"] == str(outcome.artifact.id)
    assert generated["reused"] is False
    assert generated["signed"] is False
    completed = json.loads(events[3].envelope_json)["payload"]
    assert completed["validated"] is True
    assert set(completed["blocked_checks"]) == {"security_scan", "vulnerability_scan"}


async def test_a_reused_build_says_so_in_the_event(session, test_org):
    project_id = uuid.uuid4()
    await _generate(session, test_org.id, project_id=project_id)
    session.commit()
    second = await _generate(session, test_org.id, project_id=project_id)
    session.commit()
    event = session.exec(
        select(OutboxEvent)
        .where(OutboxEvent.resource_id == str(second.generation_id))
        .where(OutboxEvent.event_type == topics.MCP_GENERATED)
    ).one()
    assert json.loads(event.envelope_json)["payload"]["reused"] is True


async def test_a_rejected_artifact_is_stored_rather_than_thrown_away(
    session, test_org, monkeypatch
):
    """Validation failing is a finding, not a lost build."""
    from sutr.generation import validation as validation_module
    from sutr.generation.scanning import FAILED, Finding

    async def failing(files, manifest):
        return validation_module.ValidationReport(
            checks=[
                validation_module.Check(
                    name="security_scan",
                    status=FAILED,
                    summary="A scanner objected.",
                    findings=[Finding("cve", "requirements.txt", 1, "CVE-0000-0000")],
                )
            ]
        )

    monkeypatch.setattr(pipeline.validation_module, "validate", failing)
    outcome = await _generate(session, test_org.id)
    session.commit()

    assert outcome.artifact.status == STATUS_REJECTED
    assert session.get(RuntimeArtifact, outcome.artifact.id) is not None
    ok, reason = artifacts.deployable(outcome.artifact)
    assert ok is False
    assert "security_scan" in reason


async def test_a_runtime_with_no_template_is_refused_by_name(session, test_org):
    with pytest.raises(pipeline.GenerationError) as excinfo:
        await _generate(session, test_org.id, runtime="go")
    assert excinfo.value.code == "runtime_not_supported"
    assert "NOT IMPLEMENTED" in excinfo.value.message
    assert "'go'" in excinfo.value.message


async def test_an_unknown_runtime_is_refused_differently(session, test_org):
    with pytest.raises(pipeline.GenerationError) as excinfo:
        await _generate(session, test_org.id, runtime="cobol")
    assert "Unknown runtime" in excinfo.value.message


async def test_a_project_that_compiles_to_nothing_is_refused(session, test_org):
    with pytest.raises(pipeline.GenerationError) as excinfo:
        await _generate(session, test_org.id, tools=[])
    assert excinfo.value.code == "no_tools"
