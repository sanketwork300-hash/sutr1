"""Shared fixtures for the registry and marketplace tests."""

import pytest
from sqlmodel import Session

from sutr.marketplace import projection
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User
from sutr.registry import service


@pytest.fixture(autouse=True)
def _projection_handlers():
    """The projection registers process-wide handlers; tests must not leak them."""
    projection.install()
    yield
    projection.uninstall()


@pytest.fixture(name="project")
def project_fixture(session: Session, test_org: Org):
    """A real OpenAPI project, so artifacts and deployments have something to
    hang off — the trust score looks a tool up through its project."""
    from sutr.models.openapi_project import OpenAPIProject

    project = OpenAPIProject(
        org_id=test_org.id,
        name="Refunds",
        spec_text="{}",
        ir_json='{"title": "Refunds", "version": "1.0.0"}',
        ir_version=2,
        ir_hash="a" * 64,
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@pytest.fixture(name="registered_tool")
def registered_tool_fixture(session: Session, test_org: Org, project):
    tool = service.register(
        session,
        org_id=test_org.id,
        tool_key="refunds-api",
        name="Refunds API",
        summary="Issue and track refunds.",
        description="A long enough description to count as documented for the trust score. " * 2,
        category="Finance",
        tags=["payments", "refunds"],
        regions=["eu-west-1"],
        compliance=["SOC2"],
        integration_id="customapi_refunds",
        project_id=project.id,
    )
    session.commit()
    session.refresh(tool)
    return tool


@pytest.fixture(name="validated_artifact")
def validated_artifact_fixture(session: Session, test_org: Org, project):
    """A real artifact, produced by the generator rather than hand-built.

    Going through the pipeline rather than inserting a row keeps this test
    suite honest about what a version actually carries: if the generator stops
    producing an SBOM, the version tests notice.
    """
    import asyncio

    from sutr.generation import pipeline
    from tests.test_generation.conftest import TOOLS

    outcome = asyncio.run(
        pipeline.generate(
            session,
            org_id=test_org.id,
            project_id=project.id,
            name="Refunds API",
            base_url="https://api.example.com/v1",
            token_header="X-Api-Key",
            token_format="{token}",
            tools=TOOLS,
            api_title="Refunds",
            api_version="1.0.0",
            ir_version=2,
            ir_hash="a" * 64,
        )
    )
    session.commit()
    session.refresh(outcome.artifact)
    return outcome.artifact


@pytest.fixture(name="rejected_artifact")
def rejected_artifact_fixture(session: Session, test_org: Org, validated_artifact):
    from sutr.models.runtime_artifact import STATUS_REJECTED

    validated_artifact.status = STATUS_REJECTED
    validated_artifact.validation_json = '{"validated": false, "failed": ["security_scan"]}'
    session.add(validated_artifact)
    session.commit()
    session.refresh(validated_artifact)
    return validated_artifact


@pytest.fixture(name="second_user")
def second_user_fixture(session: Session, test_org: Org):
    """A second admin, so four-eyes approval is possible in this org."""
    user = User(email="approver@example.com", hashed_password="x", email_verified=True)
    session.add(user)
    session.flush()
    session.add(OrgMembership(user_id=user.id, org_id=test_org.id, role="admin"))
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture(name="other_org")
def other_org_fixture(session: Session, test_user: User):
    org = Org(name="Other", slug="other-registry", owner_user_id=test_user.id)
    session.add(org)
    session.commit()
    session.refresh(org)
    return org


def publish(session: Session, tool, *, decided_by=None, visibility="public"):
    """Drive a tool from DRAFT to PUBLISHED the way the state machine requires.

    Written out rather than hidden behind a fixture flag: the path a tool takes
    to publication is the thing under test in several places, and a helper that
    skipped states would let a broken transition table pass.
    """
    from sutr.models.registry_change_request import STATUS_PENDING
    from sutr.registry import lifecycle

    for target in (
        lifecycle.API_UPLOADED,
        lifecycle.TRANSLATING,
        lifecycle.IR_READY,
        lifecycle.METADATA_READY,
        lifecycle.GENERATING_MCP,
        lifecycle.VALIDATING,
        lifecycle.DEPLOYING,
        lifecycle.DEPLOYED,
    ):
        service.transition(session, tool, target)
    if visibility != tool.visibility:
        request = service.request_visibility(session, tool, visibility=visibility)
        service.decide(session, request, approve=True, decided_by_user_id=decided_by)
    review = service.transition(session, tool, lifecycle.UNDER_REVIEW)
    assert review.change_request is not None
    assert review.change_request.status == STATUS_PENDING
    service.decide(session, review.change_request, approve=True, decided_by_user_id=decided_by)
    service.transition(session, tool, lifecycle.APPROVED)
    publish_request = service.transition(session, tool, lifecycle.PUBLISHED)
    assert publish_request.change_request is not None
    service.decide(
        session, publish_request.change_request, approve=True, decided_by_user_id=decided_by
    )
    session.commit()
    session.refresh(tool)
    return tool
