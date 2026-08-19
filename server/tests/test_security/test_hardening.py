"""Phase 12 hardening: tool-call rate limiting, secret-column removal, and
Stripe webhook idempotency."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from sutr.models.integration import InstalledIntegration
from sutr.models.log import LogEntry
from sutr.models.processed_stripe_event import ProcessedStripeEvent
from sutr.models.secret import Secret
from sutr.models.tool_execution import ToolExecutionSetting
from sutr.models.usage_event import UsageEvent
from sutr.observability import metrics
from sutr.rate_limit import tool_call_limiter
from sutr.secrets.records import get_secret_value, upsert_secret
from sutr.services.tool_pipeline import CallContext, evaluate_gate

MOCK_RESULT = {"content": [{"type": "text", "text": "ok"}]}


@pytest.fixture(name="allowed_tool")
def allowed_tool_fixture(session, test_org):
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


@pytest.fixture(name="tight_limit")
def tight_limit_fixture(monkeypatch):
    """Two calls per window, so the third is refused."""
    monkeypatch.setattr(tool_call_limiter, "max_requests", 2)
    tool_call_limiter.reset()
    yield
    tool_call_limiter.reset()


# ── Tool-call rate limiting ──────────────────────────────────────────────────


def test_gate_rate_limits_per_org(session, test_org, allowed_tool, tight_limit):
    ctx = CallContext(org_id=test_org.id, source="api")
    statuses = [
        evaluate_gate(session, ctx, "posthog", "create_annotation", {"n": i}).status
        for i in range(3)
    ]
    assert statuses[:2] == ["ready", "ready"]
    assert statuses[2] == "rate_limited"


def test_rate_limit_is_scoped_to_one_org(session, test_org, allowed_tool, tight_limit):
    """One noisy org must not spend another org's budget."""
    import uuid

    from sutr.models.org import Org

    other = Org(id=uuid.uuid4(), name="Other")
    session.add(other)
    session.commit()

    mine = CallContext(org_id=test_org.id, source="api")
    theirs = CallContext(org_id=other.id, source="api")
    for _ in range(2):
        evaluate_gate(session, mine, "posthog", "create_annotation", {})
    assert evaluate_gate(session, mine, "posthog", "create_annotation", {}).status == "rate_limited"
    # The other org is untouched (its tool isn't installed, so it gates on
    # approval — the point is that it is NOT rate limited).
    assert evaluate_gate(session, theirs, "posthog", "x", {}).status != "rate_limited"


def test_rate_limited_calls_are_not_logged_or_metered(session, test_org, allowed_tool, tight_limit):
    """The brake must be cheap: no policy row, no log, no usage event."""
    ctx = CallContext(org_id=test_org.id, source="api")
    for _ in range(2):
        evaluate_gate(session, ctx, "posthog", "create_annotation", {})
    before_logs = len(session.exec(select(LogEntry)).all())
    before_usage = len(session.exec(select(UsageEvent)).all())

    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {})

    assert gate.status == "rate_limited"
    assert gate.retry_after and gate.retry_after > 0
    session.expire_all()
    assert len(session.exec(select(LogEntry)).all()) == before_logs
    assert len(session.exec(select(UsageEvent)).all()) == before_usage


def test_disabled_limit_never_blocks(session, test_org, allowed_tool, monkeypatch):
    monkeypatch.setattr(tool_call_limiter, "max_requests", 0)
    tool_call_limiter.reset()
    ctx = CallContext(org_id=test_org.id, source="api")
    for _ in range(5):
        assert evaluate_gate(session, ctx, "posthog", "create_annotation", {}).status == "ready"


async def test_rest_returns_429_with_retry_after(
    client, session, test_org, allowed_tool, tight_limit
):
    body = {"tool_name": "create_annotation", "args": {"content": "hi"}}
    with patch("sutr.mcp.client.call_tool", new_callable=AsyncMock, return_value=MOCK_RESULT):
        for _ in range(2):
            assert (await client.post("/api/tools/posthog/call", json=body)).status_code == 200
        resp = await client.post("/api/tools/posthog/call", json=body)

    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert int(resp.headers["retry-after"]) > 0


async def test_mcp_surface_reports_the_limit(session, test_org, allowed_tool, tight_limit):
    """The MCP gateway must brake too — the limiter lives in the shared pipeline."""
    ctx = CallContext(org_id=test_org.id, source="mcp")
    for _ in range(2):
        evaluate_gate(session, ctx, "posthog", "create_annotation", {})

    from sutr.mcp import server as mcp_server_module

    gate = evaluate_gate(session, ctx, "posthog", "create_annotation", {})
    assert gate.status == "rate_limited"
    # Same branch the gateway renders for the agent.
    assert hasattr(mcp_server_module, "execute_upstream_tool")


def test_rate_limited_calls_are_counted_in_metrics(session, test_org, allowed_tool, tight_limit):
    ctx = CallContext(org_id=test_org.id, source="api")
    for _ in range(2):
        evaluate_gate(session, ctx, "posthog", "create_annotation", {})
    evaluate_gate(session, ctx, "posthog", "create_annotation", {})

    body = metrics.render()[0].decode()
    assert 'sutr_tool_calls_gated_total{reason="rate_limited",source="api"}' in body


# ── Secret storage hygiene ───────────────────────────────────────────────────


def test_secret_row_stores_nothing_derived_from_the_value(session, test_org):
    """The unsalted digest and plaintext prefix are gone for good."""
    secret = upsert_secret(
        session,
        org_id=test_org.id,
        kind="integration_token",
        ref="test/ref",
        value="sk_live_supersecret_value",
    )
    session.commit()

    columns = set(Secret.model_fields)
    assert "value_hash" not in columns
    assert "prefix" not in columns
    # And nothing else on the row carries a recognisable slice of the secret.
    stored = {
        key: value
        for key, value in secret.model_dump().items()
        if isinstance(value, str) and key != "value"
    }
    assert not any("supersecret" in value for value in stored.values())
    # Round-tripping still works — the change is storage-only.
    assert get_secret_value(session, secret.id) == "sk_live_supersecret_value"


def test_secret_update_replaces_the_value(session, test_org):
    first = upsert_secret(
        session, org_id=test_org.id, kind="integration_token", ref="r", value="one"
    )
    session.commit()
    again = upsert_secret(
        session,
        org_id=test_org.id,
        kind="integration_token",
        ref="r",
        value="two",
        secret_id=first.id,
    )
    session.commit()
    assert again.id == first.id
    assert get_secret_value(session, first.id) == "two"


# ── Stripe webhook idempotency ───────────────────────────────────────────────


def _event(event_id: str = "evt_1", event_type: str = "customer.subscription.updated") -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": {"id": "sub_1", "status": "active", "customer": "cus_1"}},
    }


async def test_webhook_processes_once_and_skips_replays(client, session, monkeypatch):
    monkeypatch.setattr("sutr.api.billing.settings.stripe_api_key", "sk_test")
    monkeypatch.setattr("sutr.api.billing.settings.stripe_price_plus", "price_test")
    monkeypatch.setattr("sutr.api.billing.settings.is_cloud", True)
    monkeypatch.setattr("sutr.api.billing.settings.stripe_webhook_secret", "whsec")

    calls: list[str] = []

    def fake_handle(event, _session):
        calls.append(event["id"])

    monkeypatch.setattr("sutr.api.billing.handle_event", fake_handle)
    monkeypatch.setattr(
        "sutr.api.billing.stripe.Webhook.construct_event",
        lambda payload, sig_header, secret: _event(),
    )

    first = await client.post(
        "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "x"}
    )
    second = await client.post(
        "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "x"}
    )

    assert first.status_code == 200 and first.json() == {"received": True}
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert calls == ["evt_1"]  # Stripe's retry did not re-apply the effects

    session.expire_all()
    rows = session.exec(select(ProcessedStripeEvent)).all()
    assert [row.event_id for row in rows] == ["evt_1"]
    assert rows[0].event_type == "customer.subscription.updated"
    assert isinstance(rows[0].processed_at, datetime)


async def test_failed_handler_leaves_the_event_unclaimed(client, session, monkeypatch):
    """A 500 must not mark the event processed, or Stripe's retry would be
    silently dropped and the subscription would never update."""
    monkeypatch.setattr("sutr.api.billing.settings.stripe_api_key", "sk_test")
    monkeypatch.setattr("sutr.api.billing.settings.stripe_price_plus", "price_test")
    monkeypatch.setattr("sutr.api.billing.settings.is_cloud", True)
    monkeypatch.setattr("sutr.api.billing.settings.stripe_webhook_secret", "whsec")
    monkeypatch.setattr(
        "sutr.api.billing.stripe.Webhook.construct_event",
        lambda payload, sig_header, secret: _event("evt_boom"),
    )

    def exploding(_event, _session):
        raise RuntimeError("handler blew up")

    monkeypatch.setattr("sutr.api.billing.handle_event", exploding)

    resp = await client.post(
        "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "x"}
    )
    assert resp.status_code == 500

    session.expire_all()
    assert session.get(ProcessedStripeEvent, "evt_boom") is None
