"""Telling prose from a picture of a table.

A PDF has no idea it contains one. `pdftotext` flattens a two-column page into
lines with the gap preserved, an architecture diagram into lines of arrows, and
a table of contents into dot leaders — and every one of those lines looks, to a
pattern that reads prose, exactly like a sentence.

The reason this matters is the citation guarantee. A "business rule" citing two
table cells that landed on the same line is a citation nobody can check, which
makes all the good citations less believable too.
"""

from sutr.documentation.chunking import chunk_elements, split_sentences
from sutr.documentation.extraction.glossary import extract_terms
from sutr.documentation.extraction.layout import (
    is_layout_noise,
    is_running_header,
    looks_like_prose,
)
from sutr.documentation.extraction.rules import RuleBasedExtractor
from sutr.documentation.extraction.workflows import extract_workflows
from sutr.documentation.parsing import parse_document
from sutr.models.document import KIND_TEXT
from sutr.models.document_chunk import DocumentChunk


def test_a_column_gap_marks_a_line_as_layout():
    """The single most reliable signal: three spaces between two words is a
    gap between columns, not a gap between clauses."""
    assert is_layout_noise("Bootstrap: one authoritative source        differences reconciled")
    assert not is_layout_noise("Refunds are allowed only within 30 days of purchase.")


def test_arrows_dot_leaders_and_pipes_are_layout():
    assert is_layout_noise("Parse → Understand → Translate")
    assert is_layout_noise("Appendix: Technology Stack .................... 27")
    assert is_layout_noise("Name | Type | Required")


def test_a_running_header_is_not_a_section_title():
    assert is_running_header("DEVELOP ER LOW-LEVEL DESIGN · C ONDENSED")
    assert not is_running_header("Refund Eligibility")


def test_prose_needs_words_and_lower_case():
    assert looks_like_prose("a reversal of a card payment initiated by the bank")
    assert not looks_like_prose("background (Kafka)")  # too few words
    assert not looks_like_prose("PCI DSS SOC 2 ISO 27001 GDPR")  # a list of labels


def test_split_sentences_joins_wrapped_lines_but_keeps_column_gaps():
    """Both halves matter. Joining hard-wrapped lines is what makes a
    prohibition findable; keeping the wide gap is what stops a table row from
    being mistaken for one."""
    wrapped = "Refunds are not permitted for digital goods that have\nbeen downloaded."
    assert split_sentences(wrapped) == [
        "Refunds are not permitted for digital goods that have been downloaded."
    ]

    columns = "Bootstrap        one authoritative source is chosen."
    assert is_layout_noise(split_sentences(columns)[0])


def _chunks(text: str) -> list[DocumentChunk]:
    parsed = parse_document(KIND_TEXT, text.encode())
    return [
        DocumentChunk(
            position=chunk.position,
            section_path=chunk.section_path,
            element_type=chunk.element_type,
            text=chunk.text,
            start_offset=chunk.start_offset,
            end_offset=chunk.end_offset,
        )
        for chunk in chunk_elements(parsed.elements)
    ]


# A page of a real technical PDF as `pdftotext` renders it: a table, a
# diagram, and a contents entry, none of which is prose.
FLATTENED_PDF_PAGE = """Platform Reference

Consistency         PostgreSQL and Neo4j = ACID; Kafka is eventual
Ledger              immutable, append-only (usage charge, credit)
Retention           seven years        must be encrypted at rest

Pipeline: Parse → Understand → Translate → Generate → Host

Appendix A: Complete Technology Stack ..................... 27
"""


def test_a_flattened_table_produces_no_rules_terms_or_workflows():
    chunks = _chunks(FLATTENED_PDF_PAGE)
    result = RuleBasedExtractor().extract(chunks)
    assert result.rules == []
    assert result.terms == []
    assert result.workflows == []


def test_the_same_content_written_as_prose_is_extracted_normally():
    """The check has to reject the *layout*, not the subject matter. If it
    also silenced the prose version, it would be trading one failure for a
    quieter one."""
    prose = (
        "Reference\n\n"
        "Retention data must be encrypted at rest for seven years.\n\n"
        "Ledger means an immutable, append-only record of every usage charge and credit.\n"
    )
    result = RuleBasedExtractor().extract(_chunks(prose))
    assert any("encrypted at rest" in rule.action for rule in result.rules)
    assert any(term.term == "Ledger" for term in result.terms)


def test_layout_rejection_is_shared_by_all_three_extractors():
    chunks = _chunks(FLATTENED_PDF_PAGE)
    assert extract_workflows(chunks) == []
    assert extract_terms(chunks) == []
