"""DOCX, using the standard library.

A .docx is a ZIP holding `word/document.xml` — so `zipfile` plus
`xml.etree.ElementTree` reads it without `python-docx`. That is not a clever
trick, it is what the format is; taking a dependency to unzip a zip would be
hard to justify.

Paragraph styles carry the structure: `Heading 1`..`Heading 6` become titles,
`List Paragraph` becomes a list item, everything else is prose. Tables are read
row by row so a limits table does not vanish.
"""

import io
import xml.etree.ElementTree as ElementTree
import zipfile

from sutr.documentation.parsing.base import (
    LIST,
    NARRATIVE,
    TABLE,
    TITLE,
    Element,
    ParseError,
    ParseResult,
)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCUMENT_PART = "word/document.xml"


def _paragraph_text(paragraph) -> str:
    """A paragraph's visible text, from its runs.

    Word splits a sentence across runs whenever formatting changes, so a
    sentence with one bolded word arrives in three pieces and has to be
    rejoined — otherwise every extractor sees fragments.
    """
    parts = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append(" ")
    return "".join(parts).strip()


def _style_of(paragraph) -> str:
    properties = paragraph.find(f"{_W}pPr")
    if properties is None:
        return ""
    style = properties.find(f"{_W}pStyle")
    if style is None:
        return ""
    return style.get(f"{_W}val") or ""


def _heading_level(style: str) -> int:
    lowered = style.lower().replace(" ", "")
    if lowered.startswith("heading"):
        suffix = lowered[len("heading") :]
        if suffix.isdigit():
            return min(int(suffix), 6)
        return 1
    if lowered in ("title",):
        return 1
    return 0


def parse_docx(data: bytes) -> ParseResult:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ParseError("not_a_docx", "This file is not a readable .docx (it is not a ZIP).")
    if _DOCUMENT_PART not in archive.namelist():
        raise ParseError(
            "not_a_docx",
            "This ZIP has no word/document.xml, so it is not a Word document. A .doc "
            "(the older binary format) must be converted to .docx first.",
        )
    try:
        root = ElementTree.fromstring(archive.read(_DOCUMENT_PART))
    except ElementTree.ParseError as exc:
        raise ParseError("docx_parse_error", f"The document body could not be read: {exc}")

    result = ParseResult(parser="docx")
    elements: list[Element] = []
    text_parts: list[str] = []
    body = root.find(f"{_W}body")
    if body is None:
        result.degraded("empty_document", "The document has no body.")
        return result

    def emit(text: str, kind: str, level: int = 0) -> None:
        if not text:
            return
        start = sum(len(part) for part in text_parts)
        text_parts.append(text + "\n\n")
        elements.append(
            Element(
                text=text,
                element_type=kind,
                level=level,
                start_offset=start,
                end_offset=start + len(text),
            )
        )

    for child in body:
        if child.tag == f"{_W}p":
            text = _paragraph_text(child)
            style = _style_of(child)
            level = _heading_level(style)
            if level:
                emit(text, TITLE, level)
            elif "list" in style.lower():
                emit(text, LIST)
            else:
                emit(text, NARRATIVE)
        elif child.tag == f"{_W}tbl":
            rows = []
            for row in child.findall(f"{_W}tr"):
                cells = [
                    " ".join(_paragraph_text(p) for p in cell.findall(f"{_W}p")).strip()
                    for cell in row.findall(f"{_W}tc")
                ]
                if any(cells):
                    rows.append(" | ".join(cells))
            emit("\n".join(rows), TABLE)

    result.elements = elements
    result.text = "".join(text_parts)
    if result.is_empty:
        result.degraded("no_text", "The document produced no readable text.")
    return result
