"""Fixtures for the zero-trust tests."""

import pytest
from sqlmodel import Session

from sutr.models.api_key import ApiKey
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.user import User
from sutr.provisioning import identity, rules


@pytest.fixture(name="other_org")
def other_org_fixture(session: Session, test_user: User):
    org = Org(name="Other", slug="other-zero-trust", owner_user_id=test_user.id)
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    session.refresh(org)
    return org


@pytest.fixture(name="api_key")
def api_key_fixture(session: Session, test_org: Org, test_user: User):
    key = ApiKey(
        org_id=test_org.id,
        created_by_user_id=test_user.id,
        name="finance-runner",
        key_prefix="sk_test_1234",
        key_hash="0" * 64,
    )
    session.add(key)
    session.commit()
    session.refresh(key)
    return key


@pytest.fixture(name="agent")
def agent_fixture(session: Session, test_org: Org, test_user: User, api_key):
    """The LLD's worked example, as an identity: a finance agent in India."""
    created = identity.register(
        session,
        org_id=test_org.id,
        name="finance-agent",
        description="Issues refunds on behalf of the finance team.",
        attributes={"role": "finance", "region": "India", "department": "Payments"},
        api_key_id=api_key.id,
        created_by_user_id=test_user.id,
    )
    session.commit()
    session.refresh(created)
    return created


@pytest.fixture(name="refund_rule")
def refund_rule_fixture(session: Session, test_org: Org):
    """`Finance role ∧ Region=India ⇒ Allow Refund Tool` (LLD §4.3.3)."""
    rule = rules.create(
        session,
        org_id=test_org.id,
        name="finance-india-refunds",
        effect="allow",
        subject={"role": "finance", "region": "India"},
        resource={"integration_id": "payments", "tool_name": "refund_payment"},
        action="invoke",
    )
    session.commit()
    session.refresh(rule)
    return rule


def principal_for(agent):
    """A Principal for an agent identity, without going through a request."""
    import json

    return identity.Principal(
        urn=identity.urn(identity.KIND_AGENT, agent.id),
        kind=identity.KIND_AGENT,
        org_id=agent.org_id,
        display=agent.name,
        agent=agent,
        attributes=json.loads(agent.attributes_json or "{}"),
    )
