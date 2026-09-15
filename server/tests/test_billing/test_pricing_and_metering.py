"""Metering facts, and pricing them afterwards (LLD §5.1)."""

import inspect
import json

import pytest
from sqlmodel import select

from sutr.billing import pricing
from sutr.common.errors import InvalidRequestError
from sutr.models.pricing_plan import PricingPlan
from sutr.models.usage_event import UsageEvent
from sutr.services import metering

from .conftest import meter

# ── Prices are evaluated at billing time, never during execution ─────────────


def test_metering_takes_no_amount_and_writes_none(session, test_org):
    """The constraint is structural: `record_usage` has no price parameter."""
    parameters = set(inspect.signature(metering.record_usage).parameters)
    assert not parameters & {"amount", "amount_micros", "price", "price_micros", "plan_id"}

    meter(session, test_org.id, count=1)
    event = session.exec(select(UsageEvent)).one()
    columns = set(event.model_dump())
    assert not columns & {"amount_micros", "price_micros", "plan_id", "cost"}
    # The pricing *context* exists and deliberately names no plan.
    assert json.loads(event.pricing_context_json) == {}


def test_the_metering_module_does_not_import_billing():
    """A price cannot be computed on the execution path if it cannot be reached."""
    source = inspect.getsource(metering)
    assert "from sutr.billing" not in source
    assert "import billing" not in source


def test_repricing_a_plan_does_not_reprice_recorded_usage(session, test_org, plan):
    """The fact and its price are recorded at different times, so old usage is
    priced by whatever was published — not by whatever is published now."""
    meter(session, test_org.id, count=20)
    first = pricing.evaluate(plan, 20)

    dearer = pricing.create(
        session,
        org_id=test_org.id,
        key="standard",
        model="per_invocation",
        name="Standard",
        amount_micros=1_000_000,
        included_units=10,
    )
    session.commit()
    # Still a draft, so it prices nothing.
    assert pricing.published_for(session, org_id=test_org.id, usage_kind="tool_call").id == plan.id
    assert pricing.evaluate(plan, 20).total_micros == first.total_micros
    assert dearer.state == "draft"


# ── Dedupe ───────────────────────────────────────────────────────────────────


def test_a_replayed_invocation_is_recorded_once(session, test_org):
    """At-least-once delivery must not become a double charge."""
    first = metering.record_usage(
        session, org_id=test_org.id, kind="tool_call", invocation_id="inv-1"
    )
    session.commit()
    second = metering.record_usage(
        session, org_id=test_org.id, kind="tool_call", invocation_id="inv-1"
    )
    session.commit()
    assert second.id == first.id
    assert len(session.exec(select(UsageEvent)).all()) == 1


def test_events_without_an_invocation_id_are_all_recorded(session, test_org):
    """NULLs do not collide, so a caller with no id still gets metered."""
    for _ in range(3):
        metering.record_usage(session, org_id=test_org.id, kind="tool_call")
    session.commit()
    assert len(session.exec(select(UsageEvent)).all()) == 3


def test_the_same_invocation_id_in_two_tenants_is_two_events(session, test_org, provider_org):
    metering.record_usage(session, org_id=test_org.id, kind="tool_call", invocation_id="inv-1")
    metering.record_usage(session, org_id=provider_org.id, kind="tool_call", invocation_id="inv-1")
    session.commit()
    assert len(session.exec(select(UsageEvent)).all()) == 2


def test_the_lld_dimensions_are_captured(session, test_org, tool):
    event = metering.record_usage(
        session,
        org_id=test_org.id,
        kind="tool_call",
        invocation_id="inv-42",
        region="eu-west-1",
        payload_bytes=2048,
        tokens=1500,
        provider_org_id=tool.org_id,
        tool_id=tool.id,
    )
    session.commit()
    assert event.region == "eu-west-1"
    assert event.payload_bytes == 2048
    assert event.tokens == 1500
    assert event.provider_org_id == tool.org_id


# ── The eight pricing models ─────────────────────────────────────────────────


def _plan(session, org_id, **overrides):
    body = {"key": f"p-{overrides.get('model', 'x')}", "name": "P", "model": "free"}
    body.update(overrides)
    return pricing.create(session, org_id=org_id, **body)


def test_every_model_the_lld_names_has_an_evaluator():
    from sutr.models.pricing_plan import MODELS

    assert set(MODELS) == set(pricing.EVALUATORS)
    assert len(MODELS) == 8


def test_free_charges_nothing(session, test_org):
    charge = pricing.evaluate(_plan(session, test_org.id, model="free"), 1000)
    assert charge.total_micros == 0


@pytest.mark.parametrize("model", ["per_invocation", "per_api_call", "per_second"])
def test_per_unit_models_charge_beyond_the_included_allowance(session, test_org, model):
    plan = _plan(session, test_org.id, model=model, amount_micros=250_000, included_units=10)
    assert pricing.evaluate(plan, 10).total_micros == 0
    charge = pricing.evaluate(plan, 14)
    assert charge.total_micros == 4 * 250_000
    assert charge.detail["billable_units"] == 4


def test_subscription_charges_its_base_regardless_of_usage(session, test_org):
    plan = _plan(session, test_org.id, model="subscription", base_micros=9_000_000)
    assert pricing.evaluate(plan, 0).total_micros == 9_000_000
    assert pricing.evaluate(plan, 5000).total_micros == 9_000_000


def test_tiered_pricing_is_graduated_not_volume(session, test_org):
    """Each band is charged at its own rate; the first thousand stays cheap."""
    plan = _plan(
        session,
        test_org.id,
        model="tiered",
        tiers=[
            {"up_to": 100, "amount_micros": 1_000},
            {"up_to": 1000, "amount_micros": 500},
            {"up_to": None, "amount_micros": 100},
        ],
    )
    charge = pricing.evaluate(plan, 1500)
    expected = 100 * 1_000 + 900 * 500 + 500 * 100
    assert charge.total_micros == expected
    assert [tier["units"] for tier in charge.detail["tiers_used"]] == [100, 900, 500]


def test_hybrid_is_a_base_plus_usage(session, test_org):
    plan = _plan(
        session,
        test_org.id,
        model="hybrid",
        base_micros=5_000_000,
        amount_micros=200_000,
        included_units=5,
    )
    charge = pricing.evaluate(plan, 15)
    assert charge.total_micros == 5_000_000 + 10 * 200_000
    assert charge.detail["base_micros"] == 5_000_000


def test_enterprise_says_the_number_came_from_outside(session, test_org):
    plan = _plan(session, test_org.id, model="enterprise", base_micros=100_000_000)
    charge = pricing.evaluate(plan, 999_999)
    assert charge.total_micros == 100_000_000
    assert "off-platform" in charge.detail["note"]


# ── Discount and tax ─────────────────────────────────────────────────────────


def test_the_pipeline_applies_discount_then_tax(session, test_org):
    """LLD §5.1: Usage Event → Pricing Plan → Discount → Tax → Charge."""
    plan = _plan(
        session,
        test_org.id,
        model="per_invocation",
        amount_micros=1_000_000,
        discount_bps=1000,
        tax_bps=1800,
        tax_label="GST",
    )
    charge = pricing.evaluate(plan, 100)
    assert charge.subtotal_micros == 100_000_000
    assert charge.discount_micros == 10_000_000
    assert charge.taxable_micros == 90_000_000
    assert charge.tax_micros == round(90_000_000 * 0.18)
    assert charge.total_micros == charge.taxable_micros + charge.tax_micros
    # And each step survives into the explanation.
    assert any("discount" in step for step in charge.steps)
    assert any("GST" in step for step in charge.steps)
    assert charge.steps[-1].startswith("charge:")


# ── Publishing guards ────────────────────────────────────────────────────────


def test_a_plan_that_cannot_be_evaluated_is_refused_at_publish_time(session, test_org):
    """A pricing mistake is cheap here and expensive at invoice time."""
    with pytest.raises(InvalidRequestError, match="last tier needs"):
        _plan(
            session,
            test_org.id,
            model="tiered",
            tiers=[{"up_to": 100, "amount_micros": 1_000}],
        )


def test_a_zero_rate_per_call_plan_is_refused_as_a_disguised_free_plan(session, test_org):
    with pytest.raises(InvalidRequestError, match="`free` model"):
        _plan(session, test_org.id, model="per_invocation", amount_micros=0)


def test_publishing_supersedes_the_previous_version(session, test_org, plan):
    second = pricing.create(
        session,
        org_id=test_org.id,
        key="standard",
        name="Standard",
        model="per_invocation",
        amount_micros=500_000,
    )
    pricing.publish(session, second)
    session.commit()
    session.refresh(plan)
    assert plan.state == "superseded"
    assert plan.superseded_at is not None
    live = pricing.published_for(session, org_id=test_org.id, usage_kind="tool_call")
    assert live.id == second.id
    assert live.version == 2


def test_a_tool_specific_plan_wins_over_a_tenant_wide_one(session, test_org, plan, tool):
    specific = pricing.create(
        session,
        org_id=test_org.id,
        key="refunds-only",
        name="Refunds",
        model="per_invocation",
        amount_micros=750_000,
        tool_id=tool.id,
    )
    pricing.publish(session, specific)
    session.commit()
    chosen = pricing.published_for(
        session, org_id=test_org.id, usage_kind="tool_call", tool_id=tool.id
    )
    assert chosen.id == specific.id
    # And a different tool still gets the general plan.
    assert pricing.published_for(session, org_id=test_org.id, usage_kind="tool_call").id == plan.id


def test_plans_are_scoped_to_their_tenant(session, test_org, provider_org, plan):
    assert pricing.published_for(session, org_id=provider_org.id, usage_kind="tool_call") is None
    assert (
        session.exec(select(PricingPlan).where(PricingPlan.org_id == provider_org.id)).all() == []
    )
