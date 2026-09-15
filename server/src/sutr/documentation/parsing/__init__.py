"""Turning a document into typed elements, whatever the format.

Every parser produces the same `ParseResult`, so nothing downstream — chunking,
extraction, the graph — learns what format a document arrived in. That is the
same property the source connectors hold for specifications, for the same
reason: one pipeline is easier to keep correct than five.
"""

from sutr.documentation.parsing.base import (
    CODE,
    LIST,
    NARRATIVE,
    TABLE,
    TITLE,
    Element,
    ParseError,
    ParseResult,
    ParserUnavailableError,
)
from sutr.documentation.parsing.registry import (
    detect_kind,
    parse_document,
    parser_capabilities,
)

__all__ = [
    "CODE",
    "LIST",
    "NARRATIVE",
    "TABLE",
    "TITLE",
    "Element",
    "ParseError",
    "ParseResult",
    "ParserUnavailableError",
    "detect_kind",
    "parse_document",
    "parser_capabilities",
]
