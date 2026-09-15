"""What extraction produces, and what an extractor is.

Build prompt §16 fixes the field set for a business rule and adds the rule that
shapes everything here: **never lose the source citation**. So every extracted
fact carries the document, the chunk, the offsets and the sentence it came
from — not as a nicety but because a rule an agent acts on that nobody can
trace back to a sentence is a rule nobody can check.

`extractor_version` is on every fact for the same reason. A rule found by
pattern matching and one found by a language model are different kinds of
claim, and a reader has to be able to tell which they are looking at.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Citation:
    """Where a fact came from. Required, never optional."""

    document_id: str = ""
    document_name: str = ""
    chunk_id: str | None = None
    section_path: str = ""
    start_offset: int = 0
    end_offset: int = 0
    page: int | None = None
    # The sentence, verbatim. A paraphrase would be a second thing to verify.
    text: str = ""

    @property
    def location(self) -> str:
        """A human-readable pointer: section, page, offsets."""
        parts = []
        if self.section_path:
            parts.append(self.section_path)
        if self.page is not None:
            parts.append(f"page {self.page}")
        parts.append(f"chars {self.start_offset}–{self.end_offset}")
        return ", ".join(parts)


@dataclass
class ExtractedRule:
    """A business rule, structured (LLD §3.5)."""

    rule_type: str
    condition: str
    action: str
    citation: Citation
    confidence: float = 0.0
    # Which signals fired. A confidence score nobody can argue with is a
    # confidence score nobody should trust.
    signals: list[str] = field(default_factory=list)


@dataclass
class ExtractedWorkflow:
    """A process described in prose, as a graph."""

    name: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    citation: Citation
    description: str = ""
    confidence: float = 0.0


@dataclass
class ExtractedTerm:
    """A term the documentation defines."""

    term: str
    definition: str
    citation: Citation
    aliases: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class ExtractionResult:
    rules: list[ExtractedRule] = field(default_factory=list)
    workflows: list[ExtractedWorkflow] = field(default_factory=list)
    terms: list[ExtractedTerm] = field(default_factory=list)
    # What this pass could not do, so a partial extraction is visible.
    degradations: list[dict[str, Any]] = field(default_factory=list)

    def extend(self, other: "ExtractionResult") -> None:
        self.rules += other.rules
        self.workflows += other.workflows
        self.terms += other.terms
        self.degradations += other.degradations


class Extractor(ABC):
    """Turns chunks into structured facts."""

    id: str
    version: str
    display_name: str
    description: str = ""

    @abstractmethod
    def extract(self, chunks: list) -> ExtractionResult:
        """Extract from a document's chunks.

        Takes the whole list rather than one chunk at a time because workflows
        can span sections, and a step list is often a heading followed by the
        steps beneath it.
        """

    def available(self) -> tuple[bool, str | None]:
        """(usable, reason-if-not)."""
        return True, None
