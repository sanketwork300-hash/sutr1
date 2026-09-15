"""What a parser produces: typed elements, not a wall of text.

Following `Unstructured-IO/unstructured`'s partition model (see
docs/REFERENCE_MAP.md): a document becomes a sequence of typed elements —
title, narrative, list, table, code — and chunking groups those by section
afterwards. The alternative, extracting one long string and cutting it into
fixed windows, reliably slices rules in half: *"Refunds are allowed only
within"* is not a rule.

Every element keeps its offsets into the extracted text and its page where the
format has pages, because a business rule must be able to cite where it came
from (build prompt §16).
"""

from dataclasses import dataclass, field
from typing import Any

TITLE = "title"
NARRATIVE = "narrative"
LIST = "list"
TABLE = "table"
CODE = "code"


@dataclass
class Element:
    """One partitioned piece of a document.

    `start_offset`/`end_offset` bound the *region* of `ParseResult.text` this
    element came from; `text` is that region normalized — a heading without its
    `#`, a paragraph without its trailing newline. So the relationship is
    containment, not equality. Citations quote `text` and point at the region,
    which is what lets a reader find the passage in the original.
    """

    text: str
    element_type: str = NARRATIVE
    # Heading depth for titles: 1 for a top-level heading, 2 for a subheading.
    level: int = 0
    page: int | None = None
    start_offset: int = 0
    end_offset: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "element_type": self.element_type,
            "level": self.level,
            "page": self.page,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
        }


@dataclass
class ParseResult:
    """A parsed document, plus what could not be read.

    `degradations` is the LLD's §3.5 failure rule made concrete: OCR that
    cannot read a page *flags the page and continues*. A parse that lost three
    pages is not a failed parse, and it is not a clean one either — so it says
    which pages, and the job records it.
    """

    elements: list[Element] = field(default_factory=list)
    text: str = ""
    page_count: int | None = None
    parser: str = ""
    degradations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(element.text.strip() for element in self.elements)

    def degraded(self, code: str, message: str, **detail: Any) -> None:
        self.degradations.append({"code": code, "message": message, **detail})


class ParserUnavailableError(Exception):
    """This format needs something that is not installed.

    Distinct from a parse failure: the document may be perfectly fine, and the
    message names what to install rather than blaming the file.
    """

    def __init__(self, message: str, *, install_hint: str = ""):
        super().__init__(message)
        self.message = message
        self.install_hint = install_hint


class ParseError(Exception):
    """The document could not be read."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
