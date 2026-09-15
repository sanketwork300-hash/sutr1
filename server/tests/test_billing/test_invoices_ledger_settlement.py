"""Invoices, the append-only ledger, revenue share and dunning."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from sutr.billing import dunning, invoices, ledger, pricing, settlement
from sutr.common.errors import ConflictError, InvalidRequestError
from sutr.config import settings
from sutr.models.invoice import Invoice, InvoiceLine
from sutr.models.ledger_entry import LedgerEntry
from sutr.models.settlement import Settlement

from .conftest import PERIOD_END, PERIOD_START, meter


def _generate(session, org_id, **overrides):
    return invoices.generate(
        session,
        org_id=org_id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        **overrides,
    )


# ── Invoices ─────────────────────────────────────────────────────────────────


def test_an_invoice_prices_the_periods_usage(session, test_org, plan):
    meter(session, test_org.id, count=30)
    result = _generate(session, test_org.id)
    session.commit()
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.quantity == 30
    # 30 calls, 10 included, 0.25 each.
    assert line.total_micros == 20 * 250_000
    assert result.invoice.total_micros == line.total_micros


def test_every_line_records_the_plan_and_the_arithmetic(session, test_org, plan):
    meter(session, test_org.id, count=12)
    result = _generate(session, test_org.id)
    session.commit()
    line = result.lines[0]
    assert line.plan_key == "standard"
    assert line.plan_version == 1
    assert line.plan_model == "per_invocation"
    payload = result.as_dict()
    breakdown = payload["lines"][0]["breakdown"]
    assert breakdown["steps"][0].startswith("usage: 12")
    assert breakdown["steps"][-1].startswith("charge:")
    assert payload["plan_versions"] == [
        {"key": "standard", "version": 1, "model": "per_invocation"}
    ]


def test_the_same_usage_and_plans_produce_the_same_invoice(session, test_org, plan):
    """Reproducible, because the price was never stored on the usage."""
    meter(session, test_org.id, count=25)
    first = _generate(session, test_org.id)
    session.commit()
    total = first.invoice.total_micros

    invoices.void(session, first.invoice, reason="regenerating to check")
    session.commit()
    second = _generate(session, test_org.id)
    session.commit()
    assert second.invoice.total_micros == total


def test_usage_nobody_priced_is_reported_rather_than_billed_at_zero(session, test_org):
    """Silently billing unpriced usage at zero is a decision; so is dropping it."""
    meter(session, test_org.id, count=5)
    result = _generate(session, test_org.id)
    session.commit()
    assert result.lines == []
    assert result.invoice.total_micros == 0
    assert result.unpriced[0]["quantity"] == 5
    assert "No published pricing plan" in result.unpriced[0]["reason"]


def test_refusals_metered_at_zero_are_not_billed(session, test_org, plan):
    meter(session, test_org.id, count=20)
    meter(session, test_org.id, count=5, quantity=0, outcome="quota_exceeded")
    result = _generate(session, test_org.id)
    session.commit()
    assert result.lines[0].quantity == 20


def test_a_second_invoice_for_the_same_period_is_refused(session, test_org, plan):
    meter(session, test_org.id, count=5)
    _generate(session, test_org.id)
    session.commit()
    with pytest.raises(ConflictError, match="double-bill"):
        _generate(session, test_org.id)


def test_a_failed_generation_leaves_a_retryable_record(session, test_org, plan):
    """§5.1: invoice generation fails — async retry, never blocks metering."""
    meter(session, test_org.id, count=5)
    result = _generate(session, test_org.id, fail_with="the pricing service timed out")
    session.commit()
    assert result.invoice.state == "failed"
    assert result.invoice.attempts == 1
    assert "timed out" in result.invoice.failure_reason
    assert invoices.retryable(session, org_id=test_org.id)[0].id == result.invoice.id

    retried = _generate(session, test_org.id)
    session.commit()
    assert retried.invoice.id == result.invoice.id, "the same row, not a second invoice"
    assert retried.invoice.state == "draft"
    assert retried.invoice.attempts == 2
    assert retried.invoice.failure_reason == ""


def test_a_retry_does_not_double_the_lines(session, test_org, plan):
    meter(session, test_org.id, count=30)
    first = _generate(session, test_org.id)
    session.commit()
    total = first.invoice.total_micros
    invoices.void(session, first.invoice, reason="try again")
    session.commit()
    second = _generate(session, test_org.id)
    session.commit()
    assert len(session.exec(select(InvoiceLine)).all()) == 1
    assert second.invoice.total_micros == total


def test_metering_keeps_working_when_generation_fails(session, test_org, plan):
    _generate(session, test_org.id, fail_with="down")
    session.commit()
    meter(session, test_org.id, count=3)
    from sutr.models.usage_event import UsageEvent

    assert len(session.exec(select(UsageEvent)).all()) == 3


# ── The ledger ───────────────────────────────────────────────────────────────


def test_issuing_an_invoice_writes_the_ledger(session, test_org):
    plan = pricing.create(
        session,
        org_id=test_org.id,
        key="taxed",
        name="Taxed",
        model="per_invocation",
        amount_micros=1_000_000,
        tax_bps=1800,
        tax_label="GST",
    )
    pricing.publish(session, plan)
    session.commit()
    meter(session, test_org.id, count=10)
    result = _generate(session, test_org.id)
    invoices.issue(session, result.invoice)
    session.commit()

    entries = ledger.entries_for(session, org_id=test_org.id)
    kinds = {entry.kind for entry in entries}
    assert kinds == {"usage_charge", "tax"}
    assert ledger.balance(session, org_id=test_org.id).balance_micros == (
        result.invoice.total_micros
    )


def test_the_ledger_has_no_update_or_delete_path():
    import inspect

    source = inspect.getsource(ledger)
    assert "def update" not in source
    assert "def delete" not in source
    assert ledger.describe()["append_only"] is True


def test_a_correction_is_a_compensating_entry(session, test_org, plan):
    meter(session, test_org.id, count=20)
    result = _generate(session, test_org.id)
    invoices.issue(session, result.invoice)
    session.commit()

    original = ledger.entries_for(session, org_id=test_org.id)[0]
    before = ledger.balance(session, org_id=test_org.id).balance_micros
    reversal = ledger.reverse(session, original, reason="billed the wrong tenant")
    session.commit()

    assert reversal.direction != original.direction
    assert reversal.amount_micros == original.amount_micros
    assert reversal.reverses_entry_id == original.id
    assert (
        ledger.balance(session, org_id=test_org.id).balance_micros
        == before - original.amount_micros
    )
    # Both entries remain. The mistake and the correction are both visible.
    assert len(session.exec(select(LedgerEntry)).all()) == 2


def test_a_reversal_needs_a_reason_and_happens_once(session, test_org, plan):
    meter(session, test_org.id, count=20)
    result = _generate(session, test_org.id)
    invoices.issue(session, result.invoice)
    session.commit()
    entry = ledger.entries_for(session, org_id=test_org.id)[0]

    with pytest.raises(InvalidRequestError, match="needs a reason"):
        ledger.reverse(session, entry, reason="  ")
    ledger.reverse(session, entry, reason="wrong tenant")
    session.commit()
    with pytest.raises(ConflictError, match="already been reversed"):
        ledger.reverse(session, entry, reason="again")


def test_a_negative_amount_is_refused_because_direction_carries_the_sign(session, test_org):
    with pytest.raises(InvalidRequestError, match="never negative"):
        ledger.append(session, org_id=test_org.id, kind="credit", amount_micros=-5)


def test_voiding_an_invoice_reverses_its_entries(session, test_org, plan):
    """A void that left the ledger alone would leave the balance wrong."""
    meter(session, test_org.id, count=20)
    result = _generate(session, test_org.id)
    invoices.issue(session, result.invoice)
    session.commit()
    assert ledger.balance(session, org_id=test_org.id).balance_micros > 0

    invoices.void(session, result.invoice, reason="billed the wrong tenant")
    session.commit()
    assert ledger.balance(session, org_id=test_org.id).balance_micros == 0
    assert result.invoice.state == "void"
    # The invoice is still there. No financial data is ever deleted.
    assert session.get(Invoice, result.invoice.id) is not None


def test_each_entry_carries_the_balance_it_produced(session, test_org):
    ledger.append(session, org_id=test_org.id, kind="usage_charge", amount_micros=1000)
    ledger.append(session, org_id=test_org.id, kind="credit", amount_micros=400)
    third = ledger.append(session, org_id=test_org.id, kind="usage_charge", amount_micros=250)
    session.commit()
    assert third.balance_micros == 1000 - 400 + 250


# ── Revenue share ────────────────────────────────────────────────────────────


def _settle(session, provider_org, **overrides):
    return settlement.run(
        session,
        provider_org_id=provider_org.id,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        **overrides,
    )


def test_the_share_comes_from_configuration_and_is_never_hard_coded(monkeypatch):
    """Build prompt §44. Read on every run, so an operator need not restart."""
    import inspect

    monkeypatch.setattr(settings, "revenue_share_provider_bps", 7000)
    assert settlement.default_share_bps() == 7000
    source = inspect.getsource(settlement)
    assert "8000" not in source.replace("8000 is the", ""), "no literal split in the logic"


def test_a_settlement_records_the_share_that_applied(session, test_org, provider_org, tool):
    plan = pricing.create(
        session,
        org_id=test_org.id,
        key="tool-plan",
        name="Tool",
        model="per_invocation",
        amount_micros=1_000_000,
        tool_id=tool.id,
    )
    pricing.publish(session, plan)
    session.commit()
    meter(session, test_org.id, count=100, tool=tool)
    result = _generate(session, test_org.id)
    invoices.issue(session, result.invoice)
    session.commit()

    record = _settle(session, provider_org)
    session.commit()
    assert record.state == "complete"
    assert record.gross_micros == 100_000_000
    assert record.provider_share_bps == 8000
    assert record.provider_micros == 80_000_000
    assert record.platform_micros == 20_000_000
    # And the provider's payable is on the provider's own ledger.
    payable = ledger.balance(session, org_id=provider_org.id, account=ledger.ACCOUNT_PAYABLE)
    assert payable.balance_micros == -80_000_000


def test_a_per_run_override_beats_the_configured_share(session, test_org, provider_org, tool):
    record = _settle(session, provider_org, provider_share_bps=6500)
    session.commit()
    assert record.provider_share_bps == 6500


def test_changing_the_configured_share_does_not_change_an_old_settlement(
    session, provider_org, monkeypatch
):
    record = _settle(session, provider_org)
    session.commit()
    original = record.provider_share_bps
    monkeypatch.setattr(settings, "revenue_share_provider_bps", 5000)
    session.refresh(record)
    assert record.provider_share_bps == original


def test_a_failed_settlement_pauses_and_preserves_the_ledger(session, test_org, provider_org, tool):
    """§5.1: settlement fails — pause payout, preserve ledger, retry."""
    record = _settle(session, provider_org, fail_with="the payout rail is unreachable")
    session.commit()
    assert record.state == "paused"
    assert "unreachable" in record.failure_reason
    assert session.exec(select(LedgerEntry)).all() == []

    settlement.retry(session, record)
    session.commit()
    assert record.state == "complete"
    assert record.attempts == 2
    # One row, so the payable was not doubled.
    assert len(session.exec(select(Settlement)).all()) == 1


def test_settling_a_period_twice_is_refused(session, provider_org):
    _settle(session, provider_org)
    session.commit()
    with pytest.raises(ConflictError, match="double the provider's payable"):
        _settle(session, provider_org)


def test_a_draft_invoice_is_not_settled(session, test_org, provider_org, tool):
    """A draft is a calculation, not an obligation."""
    plan = pricing.create(
        session,
        org_id=test_org.id,
        key="tool-plan",
        name="Tool",
        model="per_invocation",
        amount_micros=1_000_000,
        tool_id=tool.id,
    )
    pricing.publish(session, plan)
    session.commit()
    meter(session, test_org.id, count=10, tool=tool)
    _generate(session, test_org.id)
    session.commit()

    record = _settle(session, provider_org)
    session.commit()
    assert record.gross_micros == 0


def test_a_payout_is_recorded_not_made(session, provider_org):
    record = _settle(session, provider_org)
    session.commit()
    assert settlement.serialize(record)["paid_out_by_this_platform"] is False
    assert settlement.describe()["moves_money"] is False


# ── Dunning ──────────────────────────────────────────────────────────────────


def _issued(session, org_id):
    meter(session, org_id, count=20)
    result = _generate(session, org_id)
    invoices.issue(session, result.invoice)
    session.commit()
    return result.invoice


def test_a_failed_payment_marks_the_invoice_unpaid_and_schedules_a_retry(session, test_org, plan):
    invoice = _issued(session, test_org.id)
    attempt = dunning.record_attempt(
        session, invoice=invoice, succeeded=False, failure_code="card_declined"
    )
    session.commit()
    assert invoice.state == "unpaid"
    assert attempt.attempt == 1
    assert attempt.next_attempt_at is not None
    assert dunning.exhausted(session, invoice) is False


def test_a_failed_attempt_needs_a_failure_code(session, test_org, plan):
    invoice = _issued(session, test_org.id)
    with pytest.raises(InvalidRequestError, match="failure code"):
        dunning.record_attempt(session, invoice=invoice, succeeded=False)


def test_dunning_gives_up_after_the_configured_attempts(session, test_org, plan, monkeypatch):
    monkeypatch.setattr(settings, "dunning_max_attempts", 3)
    invoice = _issued(session, test_org.id)
    for _ in range(3):
        attempt = dunning.record_attempt(
            session, invoice=invoice, succeeded=False, failure_code="insufficient_funds"
        )
    session.commit()
    assert attempt.attempt == 3
    assert attempt.next_attempt_at is None, "it stops rather than retrying forever"
    assert dunning.exhausted(session, invoice) is True
    assert invoice.state == "unpaid"


def test_a_successful_payment_marks_the_invoice_paid(session, test_org, plan):
    invoice = _issued(session, test_org.id)
    dunning.record_attempt(session, invoice=invoice, succeeded=False, failure_code="declined")
    dunning.record_attempt(session, invoice=invoice, succeeded=True, reference="ch_123")
    session.commit()
    assert invoice.state == "paid"
    assert invoice.paid_at is not None


def test_an_invoice_due_for_dunning_is_found(session, test_org, plan):
    invoice = _issued(session, test_org.id)
    dunning.record_attempt(session, invoice=invoice, succeeded=False, failure_code="declined")
    session.commit()
    assert dunning.due(session, org_id=test_org.id) == []
    later = datetime.now(timezone.utc) + timedelta(hours=settings.dunning_retry_hours + 1)
    assert [row.id for row in dunning.due(session, org_id=test_org.id, now=later)] == [invoice.id]


def test_nothing_here_collects_payment():
    assert dunning.describe()["collects_payment"] is False
    assert dunning.describe()["scheduler"] is None


def test_an_attempt_past_the_maximum_is_recorded_but_never_reschedules(
    session, test_org, plan, monkeypatch
):
    """Found live. An operator who retries anyway has made a real attempt.

    Refusing to record it would lose the fact; scheduling another would undo the
    decision to stop. So it is written down, and `next_attempt_at` stays null.
    """
    monkeypatch.setattr(settings, "dunning_max_attempts", 3)
    invoice = _issued(session, test_org.id)
    for _ in range(4):
        attempt = dunning.record_attempt(
            session, invoice=invoice, succeeded=False, failure_code="declined"
        )
    session.commit()
    assert attempt.attempt == 4
    assert attempt.next_attempt_at is None
    assert dunning.exhausted(session, invoice) is True
