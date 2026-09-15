"""The subscription lifecycle: Discover → Subscribe → Provision → Use → Renew → Cancel."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from sutr.common.errors import ConflictError
from sutr.marketplace import subscriptions
from sutr.models.tool_subscription import (
    STATE_ACTIVE,
    STATE_CANCELLED,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_SUBSCRIBED,
    ToolSubscription,
)
from sutr.registry import service

from .conftest import publish


@pytest.fixture(name="published_tool")
def published_tool_fixture(session, registered_tool, second_user):
    return publish(session, registered_tool, decided_by=second_user.id)


def test_the_full_lifecycle(session, published_tool, other_org):
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    assert subscription.state == STATE_SUBSCRIBED
    assert subscription.provider_org_id == published_tool.org_id
    assert subscriptions.entitled(session, org_id=other_org.id, tool_id=published_tool.id) is False

    subscriptions.provision(session, subscription)
    assert subscription.state == STATE_ACTIVE
    assert subscription.provisioned_at is not None
    assert subscription.renews_at is not None
    assert subscriptions.entitled(session, org_id=other_org.id, tool_id=published_tool.id) is True

    renews_at = subscription.renews_at
    subscriptions.renew(session, subscription)
    assert subscription.renewed_count == 1
    assert subscription.renews_at > renews_at

    subscriptions.cancel(session, subscription, reason="no longer needed")
    session.commit()
    assert subscription.state == STATE_CANCELLED
    assert subscriptions.entitled(session, org_id=other_org.id, tool_id=published_tool.id) is False


def test_a_tenant_holds_one_open_subscription_per_tool(session, published_tool, other_org):
    subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    session.commit()
    with pytest.raises(ConflictError, match="already subscribed"):
        subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)


def test_resubscribing_after_cancelling_writes_a_new_row(session, published_tool, other_org):
    """ "Who had access in March" stays answerable."""
    first = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    subscriptions.cancel(session, first, reason="done")
    session.commit()
    second = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    session.commit()
    assert second.id != first.id
    assert len(session.exec(select(ToolSubscription)).all()) == 2


def test_an_unpublished_tool_takes_no_subscribers(session, registered_tool, other_org):
    allowed, reason = subscriptions.subscribable(registered_tool)
    assert allowed is False
    with pytest.raises(ConflictError):
        subscriptions.subscribe(session, org_id=other_org.id, tool=registered_tool)


def test_an_archived_tool_takes_no_subscribers(session, published_tool, other_org):
    service.deprecate(session, published_tool, note="gone")
    service.archive(session, published_tool)
    session.commit()
    allowed, reason = subscriptions.subscribable(published_tool)
    assert allowed is False
    assert "archived" in reason


def test_a_deprecated_tool_keeps_its_subscribers_but_takes_no_new_ones(
    session, published_tool, other_org
):
    existing = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    subscriptions.provision(session, existing)
    service.deprecate(session, published_tool, note="Use v2.")
    session.commit()

    assert subscriptions.entitled(session, org_id=other_org.id, tool_id=published_tool.id) is True
    allowed, reason = subscriptions.subscribable(published_tool)
    assert allowed is False
    assert "not published" in reason


def test_a_provider_cannot_subscribe_to_their_own_tool(session, published_tool, test_org):
    with pytest.raises(ConflictError, match="already has access"):
        subscriptions.subscribe(session, org_id=test_org.id, tool=published_tool)


def test_provisioning_can_fail_and_says_why(session, published_tool, other_org):
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    subscriptions.provision(session, subscription, failure="no runtime in that region")
    session.commit()
    assert subscription.state == STATE_FAILED
    assert subscription.failure_reason == "no runtime in that region"
    assert subscriptions.entitled(session, org_id=other_org.id, tool_id=published_tool.id) is False


def test_a_cancelled_subscription_cannot_be_provisioned(session, published_tool, other_org):
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    subscriptions.cancel(session, subscription)
    session.commit()
    with pytest.raises(ConflictError, match="not waiting to be provisioned"):
        subscriptions.provision(session, subscription)


def test_the_price_is_snapshotted_so_a_later_change_does_not_reprice_it(
    session, published_tool, other_org, second_user
):
    request = service.request_pricing(
        session, published_tool, price={"model": "per_call", "amount_micros": 1000}
    )
    service.decide(session, request, approve=True, decided_by_user_id=second_user.id)
    session.commit()
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    session.commit()
    sold_at = subscription.pricing_id

    raise_request = service.request_pricing(
        session, published_tool, price={"model": "per_call", "amount_micros": 9000}
    )
    service.decide(session, raise_request, approve=True, decided_by_user_id=second_user.id)
    session.commit()
    session.refresh(subscription)
    assert subscription.pricing_id == sold_at


def test_a_subscription_says_this_platform_is_not_billing_it(session, published_tool, other_org):
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    session.commit()
    assert subscriptions.serialize(subscription)["billed_by_this_platform"] is False


def test_renewing_early_does_not_lose_the_remaining_days(session, published_tool, other_org):
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    subscriptions.provision(session, subscription)
    first = subscriptions.as_utc(subscription.renews_at)
    subscriptions.renew(session, subscription)
    session.commit()
    session.refresh(subscription)
    renewed = subscriptions.as_utc(subscription.renews_at)
    assert renewed >= first + timedelta(days=subscriptions.TERM_DAYS - 1)


def test_expiry_is_an_explicit_sweep_not_a_read_time_lapse(session, published_tool, other_org):
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    subscriptions.provision(session, subscription)
    subscription.renews_at = datetime.now(timezone.utc) - timedelta(days=1)
    session.add(subscription)
    session.commit()

    # Still active until somebody sweeps: an entitlement that ends silently is
    # an entitlement whose end nobody can point at.
    assert subscription.state == STATE_ACTIVE
    assert subscriptions.expire_due(session) == 1
    session.commit()
    assert subscription.state == STATE_EXPIRED


def test_a_provider_sees_who_subscribed_without_reading_across_tenants(
    session, published_tool, other_org, test_org
):
    subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    session.commit()
    rows = subscriptions.list_for_provider(session, test_org.id)
    assert [row.org_id for row in rows] == [other_org.id]
    # And the consumer's own list is theirs.
    assert subscriptions.list_for_org(session, other_org.id)
    assert subscriptions.list_for_org(session, test_org.id) == []


def test_another_tenants_subscription_is_not_found(session, published_tool, other_org, test_org):
    subscription = subscriptions.subscribe(session, org_id=other_org.id, tool=published_tool)
    session.commit()
    assert subscriptions.get(session, subscription.id, test_org.id) is None
    assert subscriptions.get(session, subscription.id, other_org.id) is not None
