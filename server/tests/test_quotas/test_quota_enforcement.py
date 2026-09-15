"""Quota enforcement (ESDS LLD §5.1, build prompt §48).

Three properties are load-bearing and each is pinned here:

1. The check happens **before** the provider is contacted.
2. A refusal returns **429** with a `Retry-After`.
3. A refused call is **not billed as a successful execution** — it is metered
   at quantity 0 so the refusal is auditable without inflating the bill.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from sutr.models.integration import InstalledIntegration
from sutr.models.quota import (
    CONCURRENT_TOOL_CALLS,
    DAILY_TOOL_CALLS,
    MONTHLY_TOOL_CALLS,
    SCOPE_INTEGRATION,
    SCOPE_TENANT,
    SCOPE_TOOL,
    Quota,
)
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.models.usage_event import KIND_TOOL_CALL, UsageEvent
from sutr.services import quota as quota_service
from sutr.services.quota import evaluate

MOCK_RESULT = {"content": [{"type": "text", "text": "done"}]}


@pytest.fixture(autouse=True)
def _clear_concurrency():
    quota_service.concurrency.reset()
    yield
    quota_service.concurrency.reset()


@pytest.fixture(name="installed_posthog")
def installed_posthog_fixture(session, test_org):
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="posthog",
            type="remote_mcp",
            url="https://mcp.posthog.com/mcp",
            auth_method="token",
            connected=True,
        )
    )
    session.add(
        ToolExecutionSetting(
            org_id=test_org.id,
            integration_id="posthog",
            tool_name="docs-search",
            mode="allow",
        )
    )
    session.commit()


def _set_quota(session, org_id, kind, limit, scope=SCOPE_TENANT, scope_id=""):
    quota = Quota(org_id=org_id, kind=kind, scope=scope, scope_id=scope_id, limit_value=limit)
    session.add(quota)
    session.commit()
    return quota


def _record_calls(
    session, org_id, count, *, integration_id="posthog", tool_name="docs-search", when=None
):
    for _ in range(count):
        session.add(
            UsageEvent(
                org_id=org_id,
                kind=KIND_TOOL_CALL,
                quantity=1,
                integration_id=integration_id,
                tool_name=tool_name,
                source="api",
                outcome="executed",
                timestamp=when or datetime.utcnow(),
            )
        )
    session.commit()


# ── Evaluation ───────────────────────────────────────────────────────────────


def test_no_quotas_configured_means_no_limits(session, test_org):
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed


def test_a_daily_call_quota_blocks_once_it_is_reached(session, test_org):
    _set_quota(session, test_org.id, DAILY_TOOL_CALLS, 2)
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed

    _record_calls(session, test_org.id, 2)
    verdict = evaluate(session, test_org.id, "posthog", "docs-search")
    assert not verdict.allowed
    assert verdict.kind == DAILY_TOOL_CALLS
    assert (verdict.used, verdict.limit) == (2, 2)
    assert verdict.retry_after > 0


def test_yesterdays_usage_does_not_count_against_todays_quota(session, test_org):
    _set_quota(session, test_org.id, DAILY_TOOL_CALLS, 1)
    _record_calls(session, test_org.id, 5, when=datetime.utcnow() - timedelta(days=2))
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed


def test_a_monthly_quota_counts_the_whole_month(session, test_org):
    _set_quota(session, test_org.id, MONTHLY_TOOL_CALLS, 3)
    _record_calls(session, test_org.id, 3, when=datetime.utcnow().replace(day=1))
    assert not evaluate(session, test_org.id, "posthog", "docs-search").allowed


def test_an_integration_scoped_quota_only_counts_that_integration(session, test_org):
    _set_quota(
        session, test_org.id, DAILY_TOOL_CALLS, 1, scope=SCOPE_INTEGRATION, scope_id="posthog"
    )
    _record_calls(session, test_org.id, 5, integration_id="linear")
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed

    _record_calls(session, test_org.id, 1, integration_id="posthog")
    assert not evaluate(session, test_org.id, "posthog", "docs-search").allowed


def test_an_integration_scoped_quota_does_not_apply_to_other_integrations(session, test_org):
    _set_quota(
        session, test_org.id, DAILY_TOOL_CALLS, 1, scope=SCOPE_INTEGRATION, scope_id="posthog"
    )
    _record_calls(session, test_org.id, 5, integration_id="posthog")
    assert evaluate(session, test_org.id, "linear", "search").allowed


def test_a_tool_scoped_quota_only_counts_that_tool(session, test_org):
    _set_quota(
        session,
        test_org.id,
        DAILY_TOOL_CALLS,
        1,
        scope=SCOPE_TOOL,
        scope_id="posthog/docs-search",
    )
    _record_calls(session, test_org.id, 3, tool_name="other-tool")
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed

    _record_calls(session, test_org.id, 1, tool_name="docs-search")
    assert not evaluate(session, test_org.id, "posthog", "docs-search").allowed


def test_a_disabled_quota_is_ignored(session, test_org):
    quota = _set_quota(session, test_org.id, DAILY_TOOL_CALLS, 1)
    _record_calls(session, test_org.id, 5)
    assert not evaluate(session, test_org.id, "posthog", "docs-search").allowed
    quota.enabled = False
    session.add(quota)
    session.commit()
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed


def test_a_zero_limit_is_treated_as_unset_not_as_block_everything(session, test_org):
    _set_quota(session, test_org.id, DAILY_TOOL_CALLS, 0)
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed


def test_concurrency_is_counted_in_flight(session, test_org):
    _set_quota(session, test_org.id, CONCURRENT_TOOL_CALLS, 1)
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed
    with quota_service.in_flight(test_org.id):
        verdict = evaluate(session, test_org.id, "posthog", "docs-search")
        assert not verdict.allowed
        assert verdict.kind == CONCURRENT_TOOL_CALLS
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed


def test_a_metric_that_is_not_recorded_is_reported_unenforceable_not_silently_enforced(
    session, test_org
):
    """Token and byte quotas can be stored, but nothing records those numbers
    per call yet — so they must neither block nor pretend to pass."""
    from sutr.models.quota import DAILY_TOKENS

    _set_quota(session, test_org.id, DAILY_TOKENS, 1)
    assert evaluate(session, test_org.id, "posthog", "docs-search").allowed


# ── Through the pipeline: 429, and no billed execution ───────────────────────


async def test_rest_returns_429_with_retry_after_and_does_not_call_upstream(
    client, session, test_org, installed_posthog
):
    _set_quota(session, test_org.id, DAILY_TOOL_CALLS, 1)
    call = AsyncMock(return_value=MOCK_RESULT)
    with patch("sutr.mcp.client.call_tool", call):
        first = await client.post(
            "/api/tools/posthog/call", json={"tool_name": "docs-search", "args": {"q": "a"}}
        )
        assert first.status_code == 200
        second = await client.post(
            "/api/tools/posthog/call", json={"tool_name": "docs-search", "args": {"q": "b"}}
        )

    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0
    body = second.json()
    assert body["error"] == "quota_exceeded"
    assert body["quota"] == DAILY_TOOL_CALLS
    assert body["limit"] == 1
    # The provider was contacted exactly once — the refusal happened first.
    assert call.await_count == 1


async def test_a_refused_call_is_metered_at_zero_not_billed_as_an_execution(
    client, session, test_org, installed_posthog
):
    _set_quota(session, test_org.id, DAILY_TOOL_CALLS, 1)
    with patch("sutr.mcp.client.call_tool", AsyncMock(return_value=MOCK_RESULT)):
        await client.post("/api/tools/posthog/call", json={"tool_name": "docs-search", "args": {}})
        await client.post("/api/tools/posthog/call", json={"tool_name": "docs-search", "args": {}})

    events = session.exec(
        select(UsageEvent).where(UsageEvent.org_id == test_org.id).order_by(UsageEvent.id)
    ).all()
    assert len(events) == 2
    executed, refused = events
    assert (executed.outcome, executed.quantity) == ("executed", 1)
    assert (refused.outcome, refused.quantity) == ("quota_exceeded", 0)
    # And the refusal does not consume the next window's allowance either.
    assert sum(e.quantity for e in events) == 1


async def test_a_refused_call_is_logged_so_it_is_visible(
    client, session, test_org, installed_posthog
):
    from sutr.models.log import LogEntry

    _set_quota(session, test_org.id, DAILY_TOOL_CALLS, 0 + 1)
    with patch("sutr.mcp.client.call_tool", AsyncMock(return_value=MOCK_RESULT)):
        await client.post("/api/tools/posthog/call", json={"tool_name": "docs-search", "args": {}})
        await client.post("/api/tools/posthog/call", json={"tool_name": "docs-search", "args": {}})
    logs = session.exec(select(LogEntry).where(LogEntry.outcome == "quota_exceeded")).all()
    assert len(logs) == 1
    assert "daily_tool_calls" in logs[0].error


async def test_the_mcp_gateway_refuses_with_an_explanation_agents_can_act_on(
    client, session, test_org, installed_posthog
):
    from sutr.services.tool_pipeline import CallContext, evaluate_gate

    _set_quota(session, test_org.id, DAILY_TOOL_CALLS, 1)
    _record_calls(session, test_org.id, 1)
    ctx = CallContext(org_id=test_org.id, source="mcp")
    gate = evaluate_gate(session, ctx, "posthog", "docs-search", {})
    assert gate.status == "quota_exceeded"
    assert gate.quota is not None
    assert "daily_tool_calls" in gate.quota.message


# ── The management API ───────────────────────────────────────────────────────


async def test_setting_listing_and_deleting_a_quota(client, session, test_org):
    resp = await client.put("/api/quotas", json={"kind": DAILY_TOOL_CALLS, "limit_value": 100})
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit_value"] == 100
    assert body["used"] == 0
    assert body["window"] == "day"

    listing = await client.get("/api/quotas")
    assert len(listing.json()) == 1

    deleted = await client.delete(f"/api/quotas/{body['id']}")
    assert deleted.status_code == 204
    assert (await client.get("/api/quotas")).json() == []


async def test_setting_the_same_quota_twice_replaces_rather_than_duplicates(client):
    await client.put("/api/quotas", json={"kind": DAILY_TOOL_CALLS, "limit_value": 10})
    await client.put("/api/quotas", json={"kind": DAILY_TOOL_CALLS, "limit_value": 20})
    listing = (await client.get("/api/quotas")).json()
    assert len(listing) == 1
    assert listing[0]["limit_value"] == 20


async def test_an_unknown_quota_kind_is_rejected(client):
    resp = await client.put("/api/quotas", json={"kind": "made_up", "limit_value": 1})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unknown_quota_kind"


async def test_a_scoped_quota_requires_a_scope_id(client):
    resp = await client.put(
        "/api/quotas",
        json={"kind": DAILY_TOOL_CALLS, "limit_value": 1, "scope": SCOPE_INTEGRATION},
    )
    assert resp.status_code == 400


async def test_a_tool_scoped_quota_requires_the_integration_and_tool(client):
    resp = await client.put(
        "/api/quotas",
        json={
            "kind": DAILY_TOOL_CALLS,
            "limit_value": 1,
            "scope": SCOPE_TOOL,
            "scope_id": "posthog",
        },
    )
    assert resp.status_code == 400


async def test_listing_shows_current_consumption(client, session, test_org):
    await client.put("/api/quotas", json={"kind": DAILY_TOOL_CALLS, "limit_value": 10})
    _record_calls(session, test_org.id, 3)
    listing = (await client.get("/api/quotas")).json()
    assert listing[0]["used"] == 3


async def test_setting_a_quota_is_audited(client, session, test_org):
    from sutr.models.audit_event import AuditEvent

    await client.put("/api/quotas", json={"kind": DAILY_TOOL_CALLS, "limit_value": 5})
    events = session.exec(select(AuditEvent).where(AuditEvent.action == "quota.set")).all()
    assert len(events) == 1
    assert "5" in events[0].summary
