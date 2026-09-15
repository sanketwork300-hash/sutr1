"""The /v1/metering, /v1/billing and /v1/settlements surfaces (LLD §5.1.12).

Including the cross-tenant negatives: money is the one place where a leak
between tenants is not a bug report, it is an incident.
"""

import uuid

from sqlmodel import select

from sutr.models.audit_event import AuditEvent
from sutr.models.org import Org
from sutr.models.org_membership import OrgMembership

from .conftest import PERIOD_END, PERIOD_START, meter

PERIOD = {"period_start": PERIOD_START.isoformat(), "period_end": PERIOD_END.isoformat()}


class _OrgClient:
    """A client bound to a second organization, one request at a time."""

    def __init__(self, client, user, org):
        self._client = client
        self._user = user
        self._org = org

    async def _call(self, method: str, *args, **kwargs):
        from sutr.dependencies import AgentAuth, get_agent_auth
        from sutr.main import app

        previous = app.dependency_overrides.get(get_agent_auth)
        app.dependency_overrides[get_agent_auth] = lambda: AgentAuth(
            user=self._user, org=self._org, api_key=None
        )
        try:
            return await getattr(self._client, method)(*args, **kwargs)
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_agent_auth, None)
            else:
                app.dependency_overrides[get_agent_auth] = previous

    async def get(self, *args, **kwargs):
        return await self._call("get", *args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._call("post", *args, **kwargs)


async def _as_other_org(client, session, test_user) -> _OrgClient:
    org = Org(name="Other Tenant", slug="other-tenant", owner_user_id=test_user.id)
    session.add(org)
    session.flush()
    session.add(OrgMembership(user_id=test_user.id, org_id=org.id, role="owner"))
    session.commit()
    session.refresh(org)
    return _OrgClient(client, test_user, org)


async def _plan(client, **overrides) -> dict:
    body = {
        "key": "standard",
        "name": "Standard",
        "model": "per_invocation",
        "amount_micros": 250_000,
        "included_units": 10,
        "publish": True,
    }
    body.update(overrides)
    response = await client.post("/v1/billing/plans", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _invoice(client, **overrides) -> dict:
    response = await client.post("/v1/billing/invoices", json={**PERIOD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ── Metering ─────────────────────────────────────────────────────────────────


async def test_a_metered_event_carries_no_price(client):
    response = await client.post(
        "/v1/metering/events",
        json={"kind": "tool_call", "integration_id": "customapi_refunds", "quantity": 1},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["priced"] is False
    assert "amount_micros" not in data
    assert data["deduplicated"] is False


async def test_the_request_body_has_no_amount_field(client):
    """A caller cannot price its own usage even by trying."""
    response = await client.post(
        "/v1/metering/events",
        json={"kind": "tool_call", "amount_micros": 9_999_999, "invocation_id": "inv-1"},
    )
    assert response.status_code == 201, response.text
    read = await client.get("/v1/billing/ledger")
    assert read.json()["data"]["entries"] == []


async def test_a_replayed_invocation_is_recorded_once(client, session):
    from sutr.models.usage_event import UsageEvent

    body = {"kind": "tool_call", "invocation_id": "inv-42", "integration_id": "customapi_refunds"}
    first = await client.post("/v1/metering/events", json=body)
    second = await client.post("/v1/metering/events", json=body)
    assert first.json()["data"]["deduplicated"] is False
    assert second.json()["data"]["deduplicated"] is True
    assert first.json()["data"]["id"] == second.json()["data"]["id"]
    assert len(session.exec(select(UsageEvent)).all()) == 1


async def test_capabilities_say_what_this_install_cannot_do(client):
    response = await client.get("/v1/metering/capabilities")
    data = response.json()["data"]
    assert data["collects_payment"] is False
    assert data["settlement"]["moves_money"] is False
    assert data["settlement"]["hard_coded"] is False
    assert data["dunning"]["scheduler"] is None
    assert set(data["pricing"]["implemented"]) == set(data["pricing"]["models"])


# ── Plans ────────────────────────────────────────────────────────────────────


async def test_a_plan_is_created_published_and_audited(client, session):
    data = await _plan(client)
    assert data["state"] == "published"
    assert data["version"] == 1
    actions = session.exec(select(AuditEvent.action)).all()
    assert "billing.plan_created" in actions


async def test_an_invalid_plan_is_refused_at_publish_time(client):
    response = await client.post(
        "/v1/billing/plans",
        json={"key": "broken", "model": "tiered", "tiers": [], "publish": True},
    )
    assert response.status_code == 400, response.text
    assert "tier" in response.json()["error"]["message"].lower()


async def test_an_unknown_pricing_model_is_refused(client):
    response = await client.post("/v1/billing/plans", json={"key": "odd", "model": "per_smile"})
    assert response.status_code == 400
    assert "per_smile" in response.json()["error"]["message"]


# ── Invoices ─────────────────────────────────────────────────────────────────


async def test_an_invoice_shows_the_arithmetic_behind_each_line(client, session, test_org):
    await _plan(client)
    meter(session, test_org.id, count=30)
    data = await _invoice(client)
    assert data["total_micros"] == 20 * 250_000
    line = data["lines"][0]
    assert line["quantity"] == 30
    assert line["breakdown"]["steps"][0].startswith("usage: 30")
    assert data["collected_by_this_platform"] is False


async def test_reading_an_invoice_returns_its_lines_and_attempts(client, session, test_org):
    await _plan(client)
    meter(session, test_org.id, count=30)
    created = await _invoice(client, issue=True)
    response = await client.get(f"/v1/billing/invoices/{created['id']}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "issued"
    assert len(data["lines"]) == 1
    assert data["payment_attempts"] == []


async def test_a_simulated_generation_failure_is_retryable(client, session, test_org):
    await _plan(client)
    meter(session, test_org.id, count=30)
    failed = await _invoice(client, simulate_failure="the pricing service timed out")
    assert failed["state"] == "failed"
    assert failed["attempts"] == 1

    retried = await _invoice(client)
    assert retried["id"] == failed["id"]
    assert retried["state"] == "draft"
    assert retried["total_micros"] == 20 * 250_000


async def test_unpriced_usage_is_reported_on_the_invoice(client, session, test_org):
    meter(session, test_org.id, count=4)
    data = await _invoice(client)
    assert data["total_micros"] == 0
    assert data["unpriced"][0]["quantity"] == 4


async def test_issuing_then_voiding_leaves_a_zero_balance(client, session, test_org):
    await _plan(client)
    meter(session, test_org.id, count=30)
    created = await _invoice(client, issue=True)
    voided = await client.post(
        f"/v1/billing/invoices/{created['id']}/void", json={"reason": "billed the wrong tenant"}
    )
    assert voided.json()["data"]["state"] == "void"

    ledger = (await client.get("/v1/billing/ledger")).json()["data"]
    assert ledger["receivable"]["balance_micros"] == 0
    assert ledger["append_only"] is True
    # The charge and its reversal are both still there.
    assert len(ledger["entries"]) == 2
    assert {"billing.invoice_generated", "billing.invoice_voided"} <= set(
        session.exec(select(AuditEvent.action)).all()
    )


async def test_a_void_without_a_reason_is_refused(client, session, test_org):
    await _plan(client)
    meter(session, test_org.id, count=30)
    created = await _invoice(client, issue=True)
    response = await client.post(f"/v1/billing/invoices/{created['id']}/void", json={"reason": ""})
    assert response.status_code == 422


async def test_a_missing_invoice_reads_as_404(client):
    response = await client.get(f"/v1/billing/invoices/{uuid.uuid4()}")
    assert response.status_code == 404


# ── Dunning ──────────────────────────────────────────────────────────────────


async def test_a_failed_payment_is_recorded_and_retried(client, session, test_org):
    await _plan(client)
    meter(session, test_org.id, count=30)
    created = await _invoice(client, issue=True)
    response = await client.post(
        f"/v1/billing/invoices/{created['id']}/payments",
        json={"succeeded": False, "failure_code": "card_declined"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["attempt"] == 1
    assert data["next_attempt_at"] is not None
    assert data["invoice_state"] == "unpaid"
    assert data["dunning_exhausted"] is False


async def test_a_successful_payment_marks_the_invoice_paid(client, session, test_org):
    await _plan(client)
    meter(session, test_org.id, count=30)
    created = await _invoice(client, issue=True)
    response = await client.post(
        f"/v1/billing/invoices/{created['id']}/payments",
        json={"succeeded": True, "reference": "ch_external_1"},
    )
    assert response.json()["data"]["invoice_state"] == "paid"


# ── Settlements ──────────────────────────────────────────────────────────────


async def test_a_settlement_reports_the_share_that_applied(client):
    response = await client.post("/v1/settlements/run", json=PERIOD)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["provider_share_bps"] == 8000
    assert data["provider_share_percent"] == 80.0
    assert data["paid_out_by_this_platform"] is False


async def test_the_share_can_be_overridden_per_run(client):
    response = await client.post("/v1/settlements/run", json={**PERIOD, "provider_share_bps": 6000})
    assert response.json()["data"]["provider_share_bps"] == 6000


async def test_an_impossible_share_is_refused(client):
    response = await client.post(
        "/v1/settlements/run", json={**PERIOD, "provider_share_bps": 12_000}
    )
    assert response.status_code == 422


async def test_a_paused_settlement_retries_on_the_same_row(client, session):
    failed = await client.post(
        "/v1/settlements/run", json={**PERIOD, "simulate_failure": "payout rail unreachable"}
    )
    record = failed.json()["data"]
    assert record["state"] == "paused"

    retried = await client.post(f"/v1/settlements/{record['id']}/retry")
    assert retried.json()["data"]["state"] == "complete"
    assert retried.json()["data"]["id"] == record["id"]
    assert retried.json()["data"]["attempts"] == 2

    listed = (await client.get("/v1/settlements")).json()["data"]["settlements"]
    assert len(listed) == 1


async def test_a_payout_is_recorded_as_external(client):
    record = (await client.post("/v1/settlements/run", json=PERIOD)).json()["data"]
    response = await client.post(
        f"/v1/settlements/{record['id']}/payout", json={"reference": "neft-2026-08"}
    )
    data = response.json()["data"]
    assert data["paid_out"] is True
    assert data["payout_reference"] == "neft-2026-08"
    assert data["paid_out_by_this_platform"] is False


# ── Cross-tenant negatives ───────────────────────────────────────────────────


async def test_another_tenants_invoice_reads_as_404(client, session, test_org, test_user):
    await _plan(client)
    meter(session, test_org.id, count=30)
    created = await _invoice(client, issue=True)
    other = await _as_other_org(client, session, test_user)
    assert (await other.get(f"/v1/billing/invoices/{created['id']}")).status_code == 404


async def test_another_tenants_invoice_cannot_be_voided_or_issued_or_paid(
    client, session, test_org, test_user
):
    await _plan(client)
    meter(session, test_org.id, count=30)
    created = await _invoice(client)
    other = await _as_other_org(client, session, test_user)
    for path, body in (
        (f"/v1/billing/invoices/{created['id']}/issue", {}),
        (f"/v1/billing/invoices/{created['id']}/void", {"reason": "not mine"}),
        (f"/v1/billing/invoices/{created['id']}/payments", {"succeeded": True}),
    ):
        assert (await other.post(path, json=body)).status_code == 404, path
    # And the original is untouched.
    mine = await client.get(f"/v1/billing/invoices/{created['id']}")
    assert mine.json()["data"]["state"] == "draft"


async def test_another_tenants_invoices_and_plans_are_not_listed(
    client, session, test_org, test_user
):
    await _plan(client)
    meter(session, test_org.id, count=30)
    await _invoice(client)
    other = await _as_other_org(client, session, test_user)
    assert (await other.get("/v1/billing/invoices")).json()["data"]["invoices"] == []
    assert (await other.get("/v1/billing/plans")).json()["data"]["plans"] == []


async def test_another_tenants_ledger_is_not_readable(client, session, test_org, test_user):
    await _plan(client)
    meter(session, test_org.id, count=30)
    await _invoice(client, issue=True)
    other = await _as_other_org(client, session, test_user)
    data = (await other.get("/v1/billing/ledger")).json()["data"]
    assert data["entries"] == []
    assert data["receivable"]["balance_micros"] == 0


async def test_a_tenant_cannot_run_another_tenants_settlement(client, session, test_user):
    other = await _as_other_org(client, session, test_user)
    response = await client.post(
        "/v1/settlements/run", json={**PERIOD, "provider_org_id": str(other._org.id)}
    )
    assert response.status_code == 400
    assert "not yours" in response.json()["error"]["message"]


async def test_another_tenants_settlement_cannot_be_retried_or_paid_out(client, session, test_user):
    record = (
        await client.post("/v1/settlements/run", json={**PERIOD, "simulate_failure": "rail down"})
    ).json()["data"]
    other = await _as_other_org(client, session, test_user)
    assert (await other.post(f"/v1/settlements/{record['id']}/retry")).status_code == 404
    assert (
        await other.post(f"/v1/settlements/{record['id']}/payout", json={"reference": "x"})
    ).status_code == 404


async def test_another_tenants_settlements_are_not_listed(client, session, test_user):
    await client.post("/v1/settlements/run", json=PERIOD)
    other = await _as_other_org(client, session, test_user)
    assert (await other.get("/v1/settlements")).json()["data"]["settlements"] == []


async def test_metering_for_another_tenant_records_against_the_caller(client, session, test_user):
    """`org_id` is taken from the credential, never from the body."""
    from sutr.models.usage_event import UsageEvent

    other = await _as_other_org(client, session, test_user)
    response = await client.post(
        "/v1/metering/events",
        json={"kind": "tool_call", "org_id": str(other._org.id), "invocation_id": "inv-x"},
    )
    assert response.status_code == 201
    events = session.exec(select(UsageEvent)).all()
    assert [event.org_id for event in events] != [other._org.id]
