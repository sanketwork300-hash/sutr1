"""Chunking: sections, not windows."""

from sutr.documentation.chunking import MAX_CHUNK_CHARS, chunk_elements, split_sentences
from sutr.documentation.parsing import parse_document
from sutr.models.document import KIND_MARKDOWN

from .conftest import POLICY


def _chunks(markdown: str):
    return chunk_elements(parse_document(KIND_MARKDOWN, markdown.encode()).elements)


def test_a_chunk_carries_the_heading_path_it_sits_under():
    chunks = _chunks(POLICY)
    paths = {chunk.section_path for chunk in chunks}
    assert "Refund Policy > Eligibility" in paths
    assert "Refund Policy > Definitions" in paths


def test_a_rule_is_not_cut_in_half():
    """ "Refunds are allowed only within" is not a rule. Fixed-size windows
    produce exactly that, which is the reason chunking follows structure."""
    chunks = _chunks(POLICY)
    joined = [chunk.text for chunk in chunks]
    assert any("Refunds are allowed only within 30 days of purchase." in text for text in joined)


def test_chunks_are_positioned_in_document_order():
    chunks = _chunks(POLICY)
    assert [chunk.position for chunk in chunks] == list(range(len(chunks)))


def test_an_oversized_section_is_split_on_sentence_boundaries():
    sentence = "Refunds must be approved by a manager. "
    body = "# Long\n\n" + sentence * 200
    chunks = _chunks(body)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= MAX_CHUNK_CHARS
        # Every piece still ends a sentence: the split respected prose, not a
        # character count.
        assert chunk.text.rstrip().endswith(".")


def test_split_sentences_keeps_abbreviations_and_decimals_together():
    text = "Refunds are capped at 99.95 percent. Approval is required e.g. by a manager. Done."
    assert split_sentences(text) == [
        "Refunds are capped at 99.95 percent.",
        "Approval is required e.g. by a manager.",
        "Done.",
    ]


def test_an_empty_document_produces_no_chunks():
    assert _chunks("") == []
