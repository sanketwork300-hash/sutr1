"""Finding the terms a provider's documentation defines.

The LLD lists these beside rules and workflows (§3.5): *Chargeback, Settlement,
Refund, Authorization Hold*. The reason they matter is practical — an agent
that does not know what "authorization hold" means *in this provider's
vocabulary* will pick the wrong tool, and the specification will not tell it.

Definitions are written in a small number of shapes, and only those are matched:

    "A chargeback is a reversal of a completed payment."
    "Settlement refers to the transfer of funds to the merchant."
    "Authorization Hold: funds reserved against a card but not captured."
    "Capture means taking payment against an existing authorization."

Deliberately not matched: any sentence containing "is". Most prose is
definitional in form and not in intent — "The response is JSON" is not a
glossary entry — so a defining verb must be paired with a term-shaped subject.
"""

import re

from sutr.documentation.extraction.base import Citation, ExtractedTerm
from sutr.documentation.extraction.layout import (
    is_layout_noise,
    looks_like_prose,
)

VERSION = "rule-based-v1"

# "X is/are/means/refers to/is defined as Y"
# The defining verbs are listed explicitly and narrowly. A bare "are" would
# match "Refunds are allowed only within 30 days", which is a rule, not a
# definition — most prose is definitional in *form* and not in intent.
_DEFINITION = re.compile(
    r"^(?:the\s+|a\s+|an\s+)?"
    r"(?P<term>[A-Z][\w][\w\s\-/']{1,48}?)"
    r"\s+(?:is\s+defined\s+as|are\s+defined\s+as|refers?\s+to|"
    r"means\b|is\s+a\s|is\s+an\s|is\s+the\s)"
    r"(?P<definition>.{15,400})$",
    re.IGNORECASE,
)

# "Term: definition" — a definition list, in prose or in Markdown.
_COLON_DEFINITION = re.compile(
    r"^(?P<term>[A-Z][\w][\w\s\-/']{1,48}?)\s*[:—–]\s+(?P<definition>.{15,400})$"
)

# Words that make a "X is Y" sentence a statement about a thing rather than a
# definition of a word.
_NOT_TERMS = frozenset(
    {
        "this",
        "that",
        "it",
        "there",
        "here",
        "the response",
        "the request",
        "the result",
        "the following",
        "note",
        "example",
        "warning",
        "tip",
        "the api",
        "the endpoint",
        "the server",
        "the client",
        "the value",
        "the field",
        "the parameter",
        "the default",
        "the format",
    }
)

MIN_DEFINITION_CHARS = 15
MAX_TERM_WORDS = 5
# A colon-definition is the shape most easily faked by layout: a table cell, a
# table-of-contents entry and a diagram row all render as `Left: Right`, so the
# right-hand side has to read like a sentence about the term rather than like
# the next column of something that used to be a picture (see `layout.py`).


def _looks_like_a_term(term: str) -> bool:
    stripped = term.strip()
    lowered = stripped.lower()
    if lowered in _NOT_TERMS or lowered.startswith(("the ", "this ", "these ")):
        return False
    # An all-caps run is a heading or a column label ("ASYNCHRONOUS"), not a
    # term someone is defining. A two-letter acronym inside a longer term is
    # fine; the whole thing being upper case is not.
    if stripped.isupper() and len(stripped) > 4:
        return False
    if len(lowered.split()) > MAX_TERM_WORDS:
        return False
    # A term is a noun phrase; a sentence fragment ending in a verb is not one.
    return bool(re.match(r"^[\w][\w\s\-/']*$", term.strip()))


# Three or more spaces in a row mean columns. `pdftotext` flattens a
# two-column page into one line with the gap preserved, so `Bootstrap: one
# authoritative source        differences are reconciled` is two cells that
# were never a sentence. A single wide gap is the most reliable signal that a
# line came from a layout rather than from prose.
_COLUMN_GAP = re.compile(r"\S {3,}\S")


def _citation(chunk, text: str) -> Citation:
    from sutr.documentation.chunking import locate

    # Whitespace-tolerant: the matched text has had hard wrapping flattened,
    # so an exact search would miss and silently cite offset 0.
    begin, end = locate(chunk.text, text)
    return Citation(
        chunk_id=str(getattr(chunk, "id", "") or "") or None,
        section_path=getattr(chunk, "section_path", ""),
        start_offset=chunk.start_offset + begin,
        end_offset=chunk.start_offset + end,
        page=getattr(chunk, "page", None),
        text=text,
    )


def extract_terms(chunks: list) -> list[ExtractedTerm]:
    from sutr.documentation.chunking import split_sentences

    found: dict[str, ExtractedTerm] = {}
    for chunk in chunks:
        if getattr(chunk, "element_type", "") == "code":
            continue
        in_glossary_section = "glossary" in (getattr(chunk, "section_path", "") or "").lower()

        for line in chunk.text.splitlines():
            stripped = line.strip().lstrip("-*• ")
            if is_layout_noise(stripped):
                continue
            match = _COLON_DEFINITION.match(stripped)
            if (
                match
                and _looks_like_a_term(match.group("term"))
                and looks_like_prose(match.group("definition"))
            ):
                _record(
                    found, chunk, stripped, match, confidence=0.8 if in_glossary_section else 0.65
                )

        for sentence in split_sentences(chunk.text):
            if is_layout_noise(sentence):
                continue
            match = _DEFINITION.match(sentence.strip())
            if (
                match
                and _looks_like_a_term(match.group("term"))
                and looks_like_prose(match.group("definition"))
            ):
                _record(
                    found,
                    chunk,
                    sentence.strip(),
                    match,
                    confidence=0.75 if in_glossary_section else 0.55,
                )

    return list(found.values())


def _record(found: dict, chunk, text: str, match, *, confidence: float) -> None:
    term = " ".join(match.group("term").split())
    definition = match.group("definition").strip().rstrip(".")
    if len(definition) < MIN_DEFINITION_CHARS:
        return
    key = term.lower()
    existing = found.get(key)
    # The most confident definition wins; a term defined twice is normal, and
    # keeping both would make the glossary contradict itself.
    if existing is not None and existing.confidence >= confidence:
        return
    found[key] = ExtractedTerm(
        term=term,
        definition=definition,
        citation=_citation(chunk, text),
        confidence=confidence,
    )
