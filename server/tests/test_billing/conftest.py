"""Fixtures for the billing tests."""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session

from sutr.billing import pricing
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.models.usage_event import KIND_TOOL_CALL
from sutr.models.user import User
from sutr.services.metering import record_usage

PERIOD_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture(name="provider_org")
def provider_org_fixture(session: Session, test_user: User):
    org = Org(name="Acme Payments", slug="acme-billing", owner_user_id=test_user.id)
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    session.refresh(org)
    return org


@pytest.fixture(name="tool")
def tool_fixture(session: Session, provider_org: Org):
    from sutr.registry import service as registry_service

    created = registry_service.register(
        session,
        org_id=provider_org.id,
        tool_key="refunds-api",
        name="Refunds API",
        integration_id="customapi_refunds",
    )
    session.commit()
    session.refresh(created)
    return created


@pytest.fixture(name="plan")
def plan_fixture(session: Session, test_org: Org):
    """A published per-invocation plan: 0.25 units a call, first 10 free."""
    created = pricing.create(
        session,
        org_id=test_org.id,
        key="standard",
        name="Standard",
        model="per_invocation",
        amount_micros=250_000,
        included_units=10,
    )
    pricing.publish(session, created)
    session.commit()
    session.refresh(created)
    return created


def meter(session: Session, org_id, *, count: int = 1, tool=None, **overrides):
    """Record `count` tool calls inside the billing period."""
    events = []
    for index in range(count):
        body = {
            "org_id": org_id,
            "kind": KIND_TOOL_CALL,
            "integration_id": "customapi_refunds",
            "tool_name": "refund_payment",
            "source": "api",
            "outcome": "executed",
        }
        if tool is not None:
            body["tool_id"] = tool.id
            body["provider_org_id"] = tool.org_id
        body.update(overrides)
        event = record_usage(session, **body)
        session.flush()
        # Every event lands inside the period the fixtures invoice.
        event.timestamp = PERIOD_START.replace(tzinfo=None)
        session.add(event)
        events.append(event)
    session.commit()
    return events
