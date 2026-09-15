"""Extraction: deterministic facts, each with a citation."""

from sutr.documentation.chunking import chunk_elements
from sutr.documentation.extraction import (
    DEFAULT_EXTRACTOR,
    LlmExtractor,
    RuleBasedExtractor,
    describe_extractors,
    get_extractor,
)
from sutr.documentation.parsing import parse_document
from sutr.models.document import KIND_MARKDOWN
from sutr.models.document_chunk import DocumentChunk

from .conftest import POLICY


def _chunks(markdown: str) -> list[DocumentChunk]:
    parsed = parse_document(KIND_MARKDOWN, markdown.encode())
    return [
        DocumentChunk(
            org_id=None,
            document_id=None,
            position=chunk.position,
            section_path=chunk.section_path,
            element_type=chunk.element_type,
            text=chunk.text,
            start_offset=chunk.start_offset,
            end_offset=chunk.end_offset,
            page=chunk.page,
        )
        for chunk in chunk_elements(parsed.elements)
    ]


def _extract(markdown: str = POLICY):
    return RuleBasedExtractor().extract(_chunks(markdown))


def test_a_conditional_rule_is_split_into_condition_and_action():
    result = _extract()
    rule = next(r for r in result.rules if "30 days" in (r.condition or ""))
    assert rule.action.startswith("Refunds are allowed")
    assert rule.condition == "only within 30 days of purchase"


def test_every_extracted_fact_carries_a_citation():
    """Build prompt §16: never lose the source citation. A rule with no
    provenance is an assertion the platform cannot defend."""
    result = _extract()
    assert result.rules and result.workflows and result.terms
    for fact in [*result.rules, *result.workflows, *result.terms]:
        assert fact.citation.text
        assert fact.citation.section_path
        assert fact.citation.end_offset > fact.citation.start_offset
        assert "chars" in fact.citation.location


def test_prohibitions_are_typed_differently_from_permissions():
    result = _extract()
    kinds = {rule.rule_type for rule in result.rules}
    assert "exceptions" in kinds  # "are not permitted for digital goods"
    assert "limits" in kinds


def test_a_numbered_procedure_becomes_a_workflow_in_order():
    result = _extract()
    workflow = next(w for w in result.workflows if "Refund" in w.name)
    labels = [node["label"] for node in workflow.nodes]
    assert labels == ["Submit Refund Request", "Verify Payment", "Approve Refund", "Issue Credit"]
    assert len(workflow.edges) == len(labels) - 1


def test_definitions_become_glossary_terms():
    result = _extract()
    terms = {term.term: term.definition for term in result.terms}
    assert "Chargeback" in terms
    assert "reversal of a card payment" in terms["Chargeback"]


def test_prose_that_states_no_rule_yields_no_rule():
    """The failure mode worth guarding is the opposite of a miss: an extractor
    that finds a business rule in every paragraph makes the whole set
    worthless."""
    result = _extract("# Notes\n\nThe weather was pleasant. We had lunch by the river.\n")
    assert result.rules == []


def test_extraction_is_deterministic():
    first, second = _extract(), _extract()
    assert [(r.rule_type, r.action) for r in first.rules] == [
        (r.rule_type, r.action) for r in second.rules
    ]


def test_the_llm_extractor_declares_itself_unavailable_rather_than_guessing():
    """It is declared and unavailable, not written against an API shape nobody
    here has exercised (build prompt §4).

    Asked to extract anyway it returns nothing and records *why* — an empty
    result with a stated reason, never invented facts.
    """
    usable, reason = LlmExtractor().available()
    assert usable is False
    assert "NOT_CONFIGURED" in reason

    result = LlmExtractor().extract(_chunks(POLICY))
    assert result.rules == [] and result.workflows == [] and result.terms == []
    assert any("NOT_CONFIGURED" in d["message"] for d in result.degradations)


def test_requesting_an_unavailable_extractor_falls_back_to_the_default():
    assert get_extractor("llm").id == DEFAULT_EXTRACTOR
    assert get_extractor("nonsense").id == DEFAULT_EXTRACTOR


def test_extractors_report_their_own_availability():
    by_id = {entry["id"]: entry for entry in describe_extractors()}
    assert by_id["rule_based"]["available"] is True
    assert by_id["llm"]["available"] is False
    assert by_id["llm"]["unavailable_reason"]
