"""Choosing a parser, and reporting which formats this deployment can read."""

import re

from sutr.documentation.parsing.base import ParseError, ParseResult
from sutr.documentation.parsing.docx_parser import parse_docx
from sutr.documentation.parsing.html_parser import parse_html
from sutr.documentation.parsing.pdf_parser import available as pdf_available
from sutr.documentation.parsing.pdf_parser import ocr_available, parse_pdf
from sutr.documentation.parsing.text import parse_text
from sutr.models.document import (
    KIND_DOCX,
    KIND_HTML,
    KIND_MARKDOWN,
    KIND_PDF,
    KIND_TEXT,
)

_MEDIA_TYPES = {
    "application/pdf": KIND_PDF,
    "text/html": KIND_HTML,
    "application/xhtml+xml": KIND_HTML,
    "text/markdown": KIND_MARKDOWN,
    "text/x-markdown": KIND_MARKDOWN,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": KIND_DOCX,
    "text/plain": KIND_TEXT,
}

_EXTENSIONS = {
    ".pdf": KIND_PDF,
    ".html": KIND_HTML,
    ".htm": KIND_HTML,
    ".md": KIND_MARKDOWN,
    ".markdown": KIND_MARKDOWN,
    ".docx": KIND_DOCX,
    ".txt": KIND_TEXT,
    ".text": KIND_TEXT,
    ".rst": KIND_TEXT,
}

# The first bytes of a file are more trustworthy than its name: a browser that
# saved an HTML page as `api-docs.txt` should still be parsed as HTML.
_MAGIC = (
    (b"%PDF-", KIND_PDF),
    (b"PK\x03\x04", KIND_DOCX),  # any zip; confirmed by the docx parser
)

# Media types that mean "I don't know". Browsers send `text/plain` for a `.md`
# file and `application/octet-stream` for anything they cannot place, so
# letting these win over the filename and the content throws away the two
# signals that actually carry information — and parsing Markdown as flat text
# loses every heading, which is what the chunker groups by.
_GENERIC_MEDIA_TYPES = frozenset({"text/plain", "application/octet-stream", "binary/octet-stream"})

# A document whose first meaningful line is an ATX heading, with at least one
# more structural marker below it, is Markdown whatever it is called.
_MARKDOWN_HEADING = re.compile(rb"^#{1,6}\s+\S", re.MULTILINE)
_MARKDOWN_STRUCTURE = re.compile(rb"^(?:#{1,6}\s+\S|[-*+]\s+\S|\d+[.)]\s+\S|```)", re.MULTILINE)


def _looks_like_markdown(content: bytes) -> bool:
    head = content[:4096]
    return bool(_MARKDOWN_HEADING.search(head)) and len(_MARKDOWN_STRUCTURE.findall(head)) >= 2


def detect_kind(*, filename: str = "", media_type: str = "", content: bytes | None = None) -> str:
    """Work out what a document is, preferring evidence over declaration."""
    if content:
        for prefix, kind in _MAGIC:
            if content.startswith(prefix):
                return kind
        head = content[:2048].lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            return KIND_HTML

    base = (media_type or "").split(";")[0].strip().lower()
    if base in _MEDIA_TYPES and base not in _GENERIC_MEDIA_TYPES:
        return _MEDIA_TYPES[base]

    lowered = (filename or "").lower()
    for suffix, kind in _EXTENSIONS.items():
        if lowered.endswith(suffix):
            return kind

    if content and _looks_like_markdown(content):
        return KIND_MARKDOWN
    if base in _MEDIA_TYPES:
        return _MEDIA_TYPES[base]
    return KIND_TEXT


def parse_document(kind: str, data: bytes, *, allow_ocr: bool = True) -> ParseResult:
    """Parse raw bytes according to the detected kind."""
    if kind == KIND_PDF:
        return parse_pdf(data, allow_ocr=allow_ocr)
    if kind == KIND_DOCX:
        return parse_docx(data)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # Documentation is text; bytes that are not any text encoding are
        # almost certainly a binary format we were told the wrong name for.
        text = data.decode("utf-8", errors="replace")
        if text.count("�") > max(len(text) // 20, 10):
            raise ParseError(
                "not_text",
                "This file is not readable as text. If it is a PDF or Word document, "
                "upload it with the right filename or media type so it is parsed correctly.",
            )

    if kind == KIND_HTML:
        return parse_html(text)
    return parse_text(text, markdown=kind == KIND_MARKDOWN)


def parser_capabilities() -> list[dict]:
    """Which formats this deployment can actually read.

    Reported rather than assumed: PDF support depends on a library or a binary
    being present, and OCR on Tesseract. A user uploading a scanned PDF should
    be able to find out beforehand why it will come back empty.
    """
    pdf_ok, pdf_reason = pdf_available()
    ocr_ok, ocr_reason = ocr_available()
    return [
        {"kind": KIND_TEXT, "available": True, "unavailable_reason": None},
        {"kind": KIND_MARKDOWN, "available": True, "unavailable_reason": None},
        {"kind": KIND_HTML, "available": True, "unavailable_reason": None},
        {"kind": KIND_DOCX, "available": True, "unavailable_reason": None},
        {"kind": KIND_PDF, "available": pdf_ok, "unavailable_reason": pdf_reason},
        {"kind": "ocr", "available": ocr_ok, "unavailable_reason": ocr_reason},
    ]
