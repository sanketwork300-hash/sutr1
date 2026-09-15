"""Classification: a label that can show its evidence."""

from sutr.documentation.classification import (
    TYPE_API_GUIDE,
    TYPE_CONTRACT,
    TYPE_POLICY,
    TYPE_RUNBOOK,
    TYPE_UNKNOWN,
    classify,
)


def test_a_refund_policy_is_classified_as_a_policy():
    result = classify(
        filename="refund-policy.pdf",
        headings=["Refund Policy", "Eligibility"],
        text="Refunds are allowed only within 30 days. Customers must provide an order number.",
    )
    assert result.document_type == TYPE_POLICY
    assert result.confidence > 0.5


def test_an_api_guide_is_not_mistaken_for_a_policy():
    result = classify(
        filename="payments-api.md",
        headings=["Authentication", "Endpoints"],
        text="POST /v1/charges accepts a JSON payload. The response includes a status code.",
    )
    assert result.document_type == TYPE_API_GUIDE


def test_a_runbook_and_a_contract_are_distinguished():
    runbook = classify(
        filename="oncall-runbook.md",
        headings=["Escalation"],
        text="Step 1: page the on-call engineer. Step 2: begin rollback and mitigation.",
    )
    contract = classify(
        filename="msa.pdf",
        headings=["Terms of Service"],
        text="The parties agree that governing law shall be that of Maharashtra. Indemnification "
        "applies as set out hereinafter.",
    )
    assert runbook.document_type == TYPE_RUNBOOK
    assert contract.document_type == TYPE_CONTRACT


def test_thin_evidence_produces_unknown_rather_than_a_guess():
    """ "Unknown" is a real answer. A label asserted from one incidental word
    is worse than no label, because everything downstream trusts it."""
    result = classify(filename="notes.txt", text="The weather was pleasant by the river.")
    assert result.document_type == TYPE_UNKNOWN
    assert result.confidence == 0.0


def test_the_label_carries_the_signals_that_produced_it():
    result = classify(
        filename="refund-policy.pdf", headings=["Policy"], text="Customers must provide proof."
    )
    assert result.signals
    assert all(f":{result.document_type}:" in signal for signal in result.signals)
    assert result.as_dict()["scores"]


def test_an_ambiguous_document_reports_lower_confidence_than_a_clear_one():
    """Confidence is the margin between the top two labels, not the top score.
    A document that scores highly for two types is genuinely ambiguous."""
    clear = classify(
        filename="refund-policy.pdf",
        headings=["Refund Policy"],
        text="Refunds must be approved. This policy is prohibited from being waived.",
    )
    ambiguous = classify(
        filename="service-agreement-api.pdf",
        headings=["Terms of Service", "Endpoints"],
        text="The parties agree the API endpoint POST /v1/charges is governed by these terms. "
        "Rate limit and payload schemas are set out hereinafter by the parties.",
    )
    assert ambiguous.confidence < clear.confidence


def test_the_title_outweighs_a_passing_mention_in_the_body():
    result = classify(
        filename="incident-runbook.md",
        text="This runbook refers to the policy document and the API guide for context.",
    )
    assert result.document_type == TYPE_RUNBOOK
