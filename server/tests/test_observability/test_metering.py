"""Usage metering: the ledger is written by the canonical pipeline (so both
surfaces meter identically), survives retention, and aggregates correctly."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from sutr.maintenance import run_maintenance_sweep
from sutr.models.integration import InstalledIntegration
from sutr.models.log import LogEntry
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.models.usage_event import KIND_DEPLOYMENT_RUNTIME, KIND_TOOL_CALL, UsageEvent
from sutr.services.metering import record_usage
from sutr.services.tool_pipeline import CallContext, evaluate_gate, execute_tool

MOCK_RESULT = {"content": [{"type": "text", "text": "done"}]}


@pytest.fixture(name="allowed_posthog")
def allowed_posthog_fixture(session, test_org):
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="posthog",
            type="remote_mcp",
            url="https://mcp.posthog.com/mcp",
            auth_method="token",
        )
    )
    session.add(
        ToolExecutionSetting(
            org_id=test_org.id,
            integration_id="posthog",
            tool_name="create_annotation",
            mode="allow",
        )
    )
    session.commit()


async def test_pipeline_meters_every_execution(session, test_org, allowed_posthog):
    ctx = CallContext(org_id=test_org.id, source="mcp", api_key_prefix="ap_test12")
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    with patch("sutr.mcp.client.call_tool", new_callable=AsyncMock, return_value=MOCK_RESULT):
        await execute_tool(ctx, "posthog", "create_annotation", {"content": "hi"}, gate)

    session.expire_all()
    event = session.exec(select(UsageEvent).where(UsageEvent.org_id == test_org.id)).one()
    assert event.kind == KIND_TOOL_CALL
    assert event.quantity == 1
    assert event.integration_id == "posthog"
    assert event.tool_name == "create_annotation"
    assert event.source == "mcp"  # attribution follows the calling surface
    assert event.outcome == "executed"
    assert event.duration_ms is not None
    assert event.api_key_prefix == "ap_test12"


async def test_failed_execution_is_metered_as_error(session, test_org, allowed_posthog):
    ctx = CallContext(org_id=test_org.id, source="api")
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})

    with patch(
        "sutr.mcp.client.call_tool",
        new_callable=AsyncMock,
        side_effect=RuntimeError("upstream down"),
    ):
        await execute_tool(ctx, "posthog", "create_annotation", {"content": "hi"}, gate)

    session.expire_all()
    event = session.exec(select(UsageEvent)).one()
    assert event.outcome == "error"  # billable: the work was attempted


async def test_gated_calls_are_not_metered(session, test_org):
    """A call blocked before execution consumed no upstream work, so it must
    not appear in the billing ledger (it is still logged and audited)."""
    session.add(
        InstalledIntegration(
            org_id=test_org.id,
            integration_id="posthog",
            type="remote_mcp",
            url="https://mcp.posthog.com/mcp",
            auth_method="token",
        )
    )
    session.commit()

    ctx = CallContext(org_id=test_org.id, source="api")
    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {"content": "hi"})
    assert gate.status == "approval_pending"

    session.expire_all()
    assert session.exec(select(UsageEvent)).first() is None
    assert session.exec(select(LogEntry)).one().outcome == "pending"


def test_retention_never_prunes_usage_events(session, test_org):
    test_org.log_retention_days = 7
    session.add(test_org)
    session.add(
        LogEntry(
            org_id=test_org.id,
            timestamp=datetime.utcnow() - timedelta(days=90),
            integration_id="posthog",
            tool_name="x",
            outcome="executed",
        )
    )
    ancient = UsageEvent(
        org_id=test_org.id,
        timestamp=datetime.utcnow() - timedelta(days=900),
        kind=KIND_TOOL_CALL,
    )
    session.add(ancient)
    session.commit()

    counts = run_maintenance_sweep()
    assert counts["logs_pruned"] == 1

    session.expire_all()
    assert session.exec(select(LogEntry)).first() is None
    assert session.exec(select(UsageEvent)).one() is not None  # billing history kept


# ── /api/usage aggregation ───────────────────────────────────────────────────


async def test_usage_summary_aggregates(client, session, test_org):
    now = datetime.utcnow()
    for i in range(3):
        record_usage(
            session,
            org_id=test_org.id,
            kind=KIND_TOOL_CALL,
            integration_id="posthog",
            tool_name="create_annotation",
            source="mcp",
            outcome="executed",
            duration_ms=100 + i,
        )
    record_usage(
        session,
        org_id=test_org.id,
        kind=KIND_TOOL_CALL,
        integration_id="github",
        tool_name="create_issue",
        source="api",
        outcome="error",
        duration_ms=900,
    )
    record_usage(
        session,
        org_id=test_org.id,
        kind=KIND_DEPLOYMENT_RUNTIME,
        quantity=5,
        source="system",
    )
    session.commit()

    resp = await client.get("/api/usage/summary")
    assert resp.status_code == 200
    body = resp.json()

    assert body["tool_calls"] == 4
    kinds = {row["kind"]: row for row in body["totals_by_kind"]}
    assert kinds[KIND_TOOL_CALL]["quantity"] == 4
    assert kinds[KIND_DEPLOYMENT_RUNTIME]["quantity"] == 5  # minutes, not events
    assert kinds[KIND_DEPLOYMENT_RUNTIME]["events"] == 1

    outcomes = {row["outcome"]: row["count"] for row in body["tool_calls_by_outcome"]}
    assert outcomes == {"executed": 3, "error": 1}
    sources = {row["source"]: row["count"] for row in body["tool_calls_by_source"]}
    assert sources == {"mcp": 3, "api": 1}
    assert body["top_integrations"][0] == {"integration_id": "posthog", "count": 3}
    assert body["top_tools"][0]["tool_name"] == "create_annotation"
    assert body["daily"][-1]["count"] == 4
    assert body["duration_ms"]["max"] == 900
    # (100 + 101 + 102 + 900) / 4 = 300.75, reported rounded to one decimal.
    assert body["duration_ms"]["avg"] == 300.8

    # The window is honoured: nothing in a past range.
    old = (now - timedelta(days=90)).isoformat()
    older = (now - timedelta(days=60)).isoformat()
    resp = await client.get(f"/api/usage/summary?start={old}&end={older}")
    assert resp.json()["tool_calls"] == 0


async def test_usage_events_listing_and_range_validation(client, session, test_org):
    record_usage(session, org_id=test_org.id, kind=KIND_TOOL_CALL, integration_id="posthog")
    session.commit()

    resp = await client.get("/api/usage/events")
    assert resp.status_code == 200
    assert resp.json()[0]["integration_id"] == "posthog"

    resp = await client.get(f"/api/usage/events?kind={KIND_DEPLOYMENT_RUNTIME}")
    assert resp.json() == []

    now = datetime.utcnow()
    resp = await client.get(
        f"/api/usage/summary?start={now.isoformat()}&end={(now - timedelta(days=1)).isoformat()}"
    )
    assert resp.status_code == 400

    resp = await client.get(f"/api/usage/summary?start={(now - timedelta(days=400)).isoformat()}")
    assert resp.status_code == 400


async def test_usage_is_org_scoped(client, session, test_org):
    """Another org's usage must never appear in this org's numbers."""
    import uuid

    from sutr.models.org import Org

    other = Org(id=uuid.uuid4(), name="Other Org")
    session.add(other)
    session.commit()
    record_usage(session, org_id=other.id, kind=KIND_TOOL_CALL, integration_id="secret_thing")
    record_usage(session, org_id=test_org.id, kind=KIND_TOOL_CALL, integration_id="mine")
    session.commit()

    body = (await client.get("/api/usage/summary")).json()
    assert body["tool_calls"] == 1
    assert [r["integration_id"] for r in body["top_integrations"]] == ["mine"]

    events = (await client.get("/api/usage/events")).json()
    assert {e["integration_id"] for e in events} == {"mine"}


async def test_usage_accepts_timezone_aware_timestamps(client, session, test_org):
    """Both the UI and the CLI send JS `toISOString()` values (…Z, offset-aware)
    while the ledger stores naive UTC. Comparing them raised TypeError → 500;
    caught by the live CLI smoke, so it is pinned here."""
    record_usage(session, org_id=test_org.id, kind=KIND_TOOL_CALL, integration_id="posthog")
    session.commit()

    start = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    resp = await client.get(f"/api/usage/summary?start={start}")
    assert resp.status_code == 200
    assert resp.json()["tool_calls"] == 1

    resp = await client.get(f"/api/usage/events?start={start}")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_usage_readable_with_an_api_key(client, session, test_user, test_org, api_key_record):
    """The CLI and SDKs authenticate with API keys, so usage must be readable
    without a human JWT — caught live when `sutr usage` returned 401."""
    from sutr.dependencies import AgentAuth, get_agent_auth, get_current_org, get_current_user
    from sutr.main import app

    record_usage(session, org_id=test_org.id, kind=KIND_TOOL_CALL, integration_id="posthog")
    session.commit()

    api_key, _plain = api_key_record
    # Key-only context: no `user`, exactly what get_agent_auth yields for a key.
    app.dependency_overrides[get_agent_auth] = lambda: AgentAuth(
        org=test_org, user=None, api_key=api_key
    )
    for human_dep in (get_current_user, get_current_org):
        app.dependency_overrides.pop(human_dep, None)
    try:
        assert (await client.get("/api/usage/summary")).json()["tool_calls"] == 1
        assert len((await client.get("/api/usage/events")).json()) == 1
    finally:
        app.dependency_overrides.pop(get_agent_auth, None)


async def test_deployment_runtime_metadata_records_deployment_id(session, test_org):
    import uuid as _uuid

    from sutr.services.metering import record_deployment_runtime

    deployment_id = _uuid.uuid4()
    record_deployment_runtime(session, org_id=test_org.id, deployment_id=deployment_id, minutes=5)
    session.commit()

    event = session.exec(select(UsageEvent)).one()
    assert event.kind == KIND_DEPLOYMENT_RUNTIME
    assert event.quantity == 5
    assert json.loads(event.metadata_json)["deployment_id"] == str(deployment_id)
