"""Fixtures for the governance tests."""

import pytest
from sqlmodel import Session

from sutr.governance import policies
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User
from sutr.registry import service as registry_service


@pytest.fixture(name="second_admin")
def second_admin_fixture(session: Session, test_org: Org):
    """A second administrator, so a policy can be approved by somebody else."""
    user = User(email="approver@example.com", hashed_password="x", email_verified=True)
    session.add(user)
    session.flush()
    session.add(OrgMembership(user_id=user.id, org_id=test_org.id, role="admin"))
    session.commit()
    session.refresh(user)
    return user


@pytest.fixture(name="other_org")
def other_org_fixture(session: Session, test_user: User):
    org = Org(name="Other", slug="other-governance", owner_user_id=test_user.id)
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    session.refresh(org)
    return org


ACCESS_DOCUMENT = {
    "rules": [
        {
            "name": "no-refunds-outside-finance",
            "effect": "deny",
            "subject": {"role": "support"},
            "resource": {"integration_id": "payments", "tool_name": "refund_payment"},
            "description": "Only finance issues refunds.",
        }
    ]
}


@pytest.fixture(name="policy")
def policy_fixture(session: Session, test_org: Org, test_user: User):
    policy, version = policies.create(
        session,
        org_id=test_org.id,
        key="refund-controls",
        name="Refund controls",
        document=ACCESS_DOCUMENT,
        created_by_user_id=test_user.id,
    )
    session.commit()
    session.refresh(policy)
    session.refresh(version)
    return policy


@pytest.fixture(name="tool")
def tool_fixture(session: Session, test_org: Org, test_user: User):
    created = registry_service.register(
        session,
        org_id=test_org.id,
        tool_key="refunds-api",
        name="Refunds API",
        summary="Issue and track refunds.",
        integration_id="customapi_refunds",
        regions=["eu-west-1"],
        created_by_user_id=test_user.id,
    )
    session.commit()
    session.refresh(created)
    return created


def activate(session: Session, policy, *, author, approver):
    """Drive a policy's first version from DRAFT to ACTIVE."""
    version = policies.get_version(session, policy.id, policy.current_version)
    policies.transition(session, policy, version, "REVIEW", actor_user_id=author)
    policies.transition(session, policy, version, "APPROVED", actor_user_id=approver)
    policies.transition(session, policy, version, "PUBLISHED", actor_user_id=approver)
    policies.activate(session, policy, version, actor_user_id=approver)
    session.commit()
    session.refresh(policy)
    session.refresh(version)
    return version


@pytest.fixture(name="validated_version")
def validated_version_fixture(session: Session, test_org: Org, tool):
    """A tool version cut from a real, validated runtime artifact.

    Built through the generation pipeline rather than inserted, so the review's
    validation and scan stages read a report that a real run produced.
    """
    import asyncio

    from sutr.generation import pipeline
    from sutr.models.openapi_project import OpenAPIProject
    from sutr.registry import versions
    from tests.test_generation.conftest import TOOLS

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
    tool.project_id = project.id
    session.add(tool)
    session.commit()

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
    version = versions.create(session, tool=tool, artifact=outcome.artifact)
    session.commit()
    session.refresh(version)
    return version


@pytest.fixture(name="clean_compliance")
def clean_compliance_fixture(
    session: Session, test_org: Org, test_user: User, second_admin, policy, monkeypatch
):
    """A compliance run with no failures, so the review can reach approval.

    The default install fails three controls **by design** — plaintext secrets,
    no configured scanner, and no active governance policy — so a workflow test
    that wanted to reach the approval stage would otherwise be testing those
    failures instead. Fixing all three is what a clean run costs, which is the
    point of the controls.
    """
    from sutr.config import settings
    from sutr.governance import compliance

    monkeypatch.setattr(settings, "secrets_backend", "db_kms")
    monkeypatch.setattr(settings, "generation_security_scan_command", "true")
    activate(session, policy, author=test_user.id, approver=second_admin.id)
    record = compliance.run(session, org_id=test_org.id, framework=compliance.ORG_CONTROLS)
    session.commit()
    assert record.failed == 0, "the fixture must produce a clean run"
    return record
