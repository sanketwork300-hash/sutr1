"""Turning chunks into structured facts.

Two extractors behind one interface. The pattern-based one is the default and
actually runs; the model-based one is declared and refuses until a provider is
configured (see `llm.py` for why it is not simply written anyway).

Every fact either extractor produces carries a citation and the version of the
extractor that produced it, so a reader can always tell what kind of claim they
are looking at.
"""

from sutr.documentation.extraction.base import (
    Citation,
    ExtractedRule,
    ExtractedTerm,
    ExtractedWorkflow,
    ExtractionResult,
    Extractor,
)
from sutr.documentation.extraction.llm import LlmExtractor
from sutr.documentation.extraction.rules import RuleBasedExtractor

_EXTRACTORS: dict[str, Extractor] = {
    extractor.id: extractor for extractor in (RuleBasedExtractor(), LlmExtractor())
}

DEFAULT_EXTRACTOR = RuleBasedExtractor.id


def get_extractor(extractor_id: str | None = None) -> Extractor:
    """The requested extractor, or the default.

    An unavailable extractor falls back to the default rather than failing the
    job: extraction that produces fewer facts is better than a document that
    cannot be processed at all, and the fallback is recorded as a degradation
    by the caller.
    """
    extractor = _EXTRACTORS.get(extractor_id or DEFAULT_EXTRACTOR)
    if extractor is None:
        return _EXTRACTORS[DEFAULT_EXTRACTOR]
    usable, _ = extractor.available()
    return extractor if usable else _EXTRACTORS[DEFAULT_EXTRACTOR]


def describe_extractors() -> list[dict]:
    entries = []
    for extractor in _EXTRACTORS.values():
        usable, reason = extractor.available()
        entries.append(
            {
                "id": extractor.id,
                "version": extractor.version,
                "display_name": extractor.display_name,
                "description": extractor.description,
                "available": usable,
                "unavailable_reason": reason,
                "default": extractor.id == DEFAULT_EXTRACTOR,
            }
        )
    return entries


__all__ = [
    "DEFAULT_EXTRACTOR",
    "Citation",
    "ExtractedRule",
    "ExtractedTerm",
    "ExtractedWorkflow",
    "ExtractionResult",
    "Extractor",
    "LlmExtractor",
    "RuleBasedExtractor",
    "describe_extractors",
    "get_extractor",
]
