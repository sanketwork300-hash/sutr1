"""Tenant-isolated telemetry (ESDS LLD §5.3), including the negatives.

The shared stack — one Prometheus, one Loki, one Tempo — holds every tenant's
data and is never exposed to a tenant. What a tenant gets instead is this: its
own calls, from this platform's own tables, scoped by the query rather than by
a filter that a later edit could drop.

Which makes the cross-tenant tests the important half of this file. Telemetry
is not money, but "how often does that company call which providers" is exactly
the kind of thing a competitor would like to know.
"""

import uuid
from datetime import datetime, timedelta

from sutr.models.log import LogEntry
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership
from sutr.observability import tenant


class _OrgClient:
    """A client bound to a second organization, one request at a time."""

    def __init__(self, client, user, org):
        self._client = client
        self._user = user
        self._org = org

    async def get(self, *args, **kwargs):
        from sutr.dependencies import AgentAuth, get_agent_auth
        from sutr.main import app

        previous = app.dependency_overrides.get(get_agent_auth)
        app.dependency_overrides[get_agent_auth] = lambda: AgentAuth(
            user=self._user, org=self._org, api_key=None
        )
        try:
            return await self._client.get(*args, **kwargs)
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_agent_auth, None)
            else:
                app.dependency_overrides[get_agent_auth] = previous


def _other_org(client, session, test_user) -> tuple[_OrgClient, Org]:
    org = Org(name="Other Tenant")
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    session.refresh(org)
    return _OrgClient(client, test_user, org), org


def _log(
    session,
    org_id,
    *,
    correlation_id="corr-1",
    outcome="executed",
    duration_ms=100,
    integration_id="stripe",
    tool_name="create_charge",
    minutes_ago=1,
    trace_id=None,
    error=None,
):
    entry = LogEntry(
        org_id=org_id,
        integration_id=integration_id,
        tool_name=tool_name,
        outcome=outcome,
        duration_ms=duration_ms,
        correlation_id=correlation_id,
        trace_id=trace_id,
        error=error,
    )
    session.add(entry)
    session.flush()
    entry.timestamp = datetime.utcnow() - timedelta(minutes=minutes_ago)
    session.add(entry)
    session.commit()
    return entry


def _window(hours: int = 24):
    return tenant.window(hours, maximum_hours=720)


# ── The summary ──────────────────────────────────────────────────────────────


def test_the_summary_counts_only_the_asking_tenants_calls(session, test_org, test_user):
    other = Org(name="Someone Else")
    session.add(other)
    session.commit()
    session.refresh(other)

    _log(session, test_org.id, duration_ms=10)
    _log(session, other.id, duration_ms=9999)
    _log(session, other.id, duration_ms=9999)

    start, end = _window()
    summary = tenant.summary(session, org_id=test_org.id, start=start, end=end)

    assert summary["calls"] == 1
    assert summary["latency_ms"]["max"] == 10


def test_errors_and_refusals_both_count_against_the_error_ratio(session, test_org):
    _log(session, test_org.id, outcome="executed")
    _log(session, test_org.id, outcome="executed")
    _log(session, test_org.id, outcome="error", error="upstream said no")
    _log(session, test_org.id, outcome="denied")

    start, end = _window()
    summary = tenant.summary(session, org_id=test_org.id, start=start, end=end)

    assert summary["calls"] == 4
    assert summary["errors"] == 2
    assert summary["error_ratio"] == 0.5
    assert summary["by_outcome"] == {"executed": 2, "error": 1, "denied": 1}


def test_percentiles_are_latencies_that_were_actually_observed(session, test_org):
    for value in (10, 20, 30, 40, 1000):
        _log(session, test_org.id, duration_ms=value)

    start, end = _window()
    latency = tenant.summary(session, org_id=test_org.id, start=start, end=end)["latency_ms"]

    # Nearest rank, no interpolation: every number here is a real measurement.
    assert latency["p50"] == 30
    assert latency["p95"] == 1000
    assert latency["max"] == 1000
    assert latency["count"] == 5


def test_a_period_with_no_calls_is_zero_rather_than_an_error(session, test_org):
    start, end = _window()
    summary = tenant.summary(session, org_id=test_org.id, start=start, end=end)

    assert summary["calls"] == 0
    assert summary["error_ratio"] == 0.0
    assert summary["latency_ms"] == {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}


def test_latency_is_broken_down_by_provider(session, test_org):
    _log(session, test_org.id, integration_id="stripe", duration_ms=50)
    _log(session, test_org.id, integration_id="stripe", duration_ms=70)
    _log(session, test_org.id, integration_id="github", duration_ms=900)

    start, end = _window()
    by_provider = tenant.summary(session, org_id=test_org.id, start=start, end=end)["by_provider"]

    assert by_provider["stripe"]["calls"] == 2
    assert by_provider["github"]["latency_ms"]["max"] == 900


def test_calls_outside_the_window_are_not_counted(session, test_org):
    _log(session, test_org.id, minutes_ago=1)
    _log(session, test_org.id, minutes_ago=60 * 48)

    start, end = _window(24)
    assert tenant.summary(session, org_id=test_org.id, start=start, end=end)["calls"] == 1


def test_a_truncated_summary_says_so(session, test_org, monkeypatch):
    """Describing a fraction of a period as though it were the whole of it is
    the kind of quiet wrongness a dashboard never recovers from."""
    monkeypatch.setattr(tenant, "MAX_SAMPLE", 2)
    for _ in range(3):
        _log(session, test_org.id)

    start, end = _window()
    summary = tenant.summary(session, org_id=test_org.id, start=start, end=end)

    assert summary["calls"] == 2
    assert summary["truncated"] is True


def test_a_call_with_no_duration_still_counts_as_a_call(session, test_org):
    """A denied call never ran, so it has no latency — but it happened."""
    _log(session, test_org.id, outcome="denied", duration_ms=None)

    start, end = _window()
    summary = tenant.summary(session, org_id=test_org.id, start=start, end=end)

    assert summary["calls"] == 1
    assert summary["latency_ms"]["count"] == 0


# ── Operations ───────────────────────────────────────────────────────────────


def test_one_operation_groups_the_calls_that_shared_a_correlation_id(session, test_org):
    """An approval granted and then executed is two requests and one operation.
    That is what the correlation id is for."""
    _log(session, test_org.id, correlation_id="op-1", outcome="pending", minutes_ago=5)
    _log(session, test_org.id, correlation_id="op-1", outcome="executed", minutes_ago=4)
    _log(session, test_org.id, correlation_id="op-2", minutes_ago=3)

    start, end = _window()
    found = tenant.operations(session, org_id=test_org.id, start=start, end=end)

    by_id = {row["correlation_id"]: row for row in found}
    assert by_id["op-1"]["steps"] == 2
    assert by_id["op-1"]["duration_ms"] == 200
    assert [row["correlation_id"] for row in found] == ["op-2", "op-1"]  # newest first


def test_an_operations_timeline_reads_in_the_order_things_happened(session, test_org):
    _log(session, test_org.id, correlation_id="op-3", tool_name="first", minutes_ago=5)
    _log(session, test_org.id, correlation_id="op-3", tool_name="second", minutes_ago=2)

    found = tenant.operation(session, org_id=test_org.id, correlation_id="op-3")

    assert [step["tool_id"] for step in found["steps"]] == ["first", "second"]


def test_an_operation_carries_the_trace_id_when_there_was_a_trace(session, test_org):
    _log(session, test_org.id, correlation_id="op-4", trace_id="a" * 32)

    found = tenant.operation(session, org_id=test_org.id, correlation_id="op-4")
    assert found["trace_id"] == "a" * 32


def test_an_operation_recorded_without_tracing_claims_no_trace(session, test_org):
    """The default install runs with tracing off. Saying otherwise would send a
    reader looking for a trace that was never recorded."""
    _log(session, test_org.id, correlation_id="op-5", trace_id=None)

    assert tenant.operation(session, org_id=test_org.id, correlation_id="op-5")["trace_id"] is None


def test_an_unknown_operation_is_none_rather_than_an_empty_timeline(session, test_org):
    assert tenant.operation(session, org_id=test_org.id, correlation_id="never-existed") is None


def test_a_stored_error_is_shown_as_stored(session, test_org):
    """Errors are redacted when they are written, so what is stored is what a
    tenant may read."""
    _log(session, test_org.id, correlation_id="op-6", outcome="error", error="connect timeout")

    step = tenant.operation(session, org_id=test_org.id, correlation_id="op-6")["steps"][0]
    assert step["error"] == "connect timeout"


# ── The window ───────────────────────────────────────────────────────────────


def test_a_window_longer_than_allowed_is_clamped_rather_than_refused():
    start, end = tenant.window(24 * 400, maximum_hours=720)
    assert round((end - start).total_seconds() / 3600) == 720


def test_no_window_means_the_default_day():
    start, end = tenant.window(None, maximum_hours=720)
    assert round((end - start).total_seconds() / 3600) == 24


# ── The API ──────────────────────────────────────────────────────────────────


async def test_the_capabilities_report_says_what_this_install_cannot_do(client):
    response = await client.get("/v1/observability/capabilities")
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["metrics"]["tenant_labels"] is False
    assert data["tenant_telemetry"]["trace_backend_queries"] is False
    assert data["tenant_telemetry"]["metrics_backend_queries"] is False
    assert data["tenant_telemetry"]["scope"] == "the calling organisation only"
    assert data["logging"]["redaction"] == "formatter"
    assert "correlation_id" in data["logging"]["fields"]


async def test_the_summary_endpoint_answers_for_the_calling_org(client, session, test_org):
    _log(session, test_org.id, duration_ms=42)

    response = await client.get("/v1/observability/summary?window_hours=1")

    assert response.status_code == 200
    assert response.json()["data"]["calls"] == 1
    assert response.json()["data"]["latency_ms"]["p50"] == 42


async def test_the_operations_endpoint_lists_the_callers_operations(client, session, test_org):
    _log(session, test_org.id, correlation_id="op-api")

    data = (await client.get("/v1/observability/operations")).json()["data"]

    assert data["count"] == 1
    assert data["operations"][0]["correlation_id"] == "op-api"


async def test_one_operation_can_be_read_by_its_correlation_id(client, session, test_org):
    _log(session, test_org.id, correlation_id="op-api-2", tool_name="charge")

    response = await client.get("/v1/observability/operations/op-api-2")

    assert response.status_code == 200
    assert response.json()["data"]["steps"][0]["tool_id"] == "charge"


async def test_an_unknown_correlation_id_is_a_404(client):
    assert (await client.get("/v1/observability/operations/nope")).status_code == 404


async def test_every_response_carries_the_standard_envelope(client):
    body = (await client.get("/v1/observability/summary")).json()
    assert set(body) == {"data", "meta"}
    assert "correlation_id" in body["meta"]


# ── Cross-tenant negatives ───────────────────────────────────────────────────


async def test_another_tenant_sees_none_of_this_tenants_calls(client, session, test_org, test_user):
    _log(session, test_org.id, correlation_id="private-op", integration_id="acme-payments")
    other_client, _ = _other_org(client, session, test_user)

    summary = (await other_client.get("/v1/observability/summary")).json()["data"]

    assert summary["calls"] == 0
    assert summary["by_provider"] == {}


async def test_another_tenant_cannot_list_this_tenants_operations(
    client, session, test_org, test_user
):
    _log(session, test_org.id, correlation_id="private-op-2")
    other_client, _ = _other_org(client, session, test_user)

    data = (await other_client.get("/v1/observability/operations")).json()["data"]

    assert data["operations"] == []
    assert data["count"] == 0


async def test_another_tenants_correlation_id_is_a_404_not_a_403(
    client, session, test_org, test_user
):
    """404, because 403 would confirm the id exists — which is itself the
    answer to "did that company make this call?"."""
    _log(session, test_org.id, correlation_id="private-op-3")
    other_client, _ = _other_org(client, session, test_user)

    response = await other_client.get("/v1/observability/operations/private-op-3")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    # The owning tenant still reads it, so the 404 is about who is asking.
    assert (await client.get("/v1/observability/operations/private-op-3")).status_code == 200


def test_the_read_model_scopes_every_query_by_organisation():
    """Structural, because a filter that is only a convention is a filter that
    will be dropped by an edit nobody reviews closely."""
    import inspect

    for function in (tenant.summary, tenant.operations, tenant.operation):
        source = inspect.getsource(function)
        assert "LogEntry.org_id == org_id" in source, function.__name__


def test_the_read_model_cannot_be_asked_for_another_organisation(session, test_org):
    """org_id is a keyword-only argument with no default: there is no call that
    forgets it and quietly reads everything."""
    import inspect

    signature = inspect.signature(tenant.summary)
    assert signature.parameters["org_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["org_id"].default is inspect.Parameter.empty

    # And an organisation with no rows reads as empty rather than as everyone's.
    start, end = _window()
    assert tenant.summary(session, org_id=uuid.uuid4(), start=start, end=end)["calls"] == 0
