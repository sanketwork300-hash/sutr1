"""Plain text and Markdown.

Markdown is the easy case and the common one: headings are explicit, lists are
explicit, and code fences are explicit. Everything the chunker needs is already
in the syntax, so nothing has to be inferred.
"""

import re

from sutr.documentation.parsing.base import CODE, LIST, NARRATIVE, TITLE, Element, ParseResult

_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_SETEXT_H1 = re.compile(r"^=+\s*$")
_SETEXT_H2 = re.compile(r"^-{2,}\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
# A short line ending without punctuation, in an otherwise prose document, is
# almost always a heading someone wrote without Markdown.
_BARE_HEADING = re.compile(r"^[A-Z][^.!?]{0,70}$")


def parse_text(content: str, *, markdown: bool = True) -> ParseResult:
    result = ParseResult(parser="markdown" if markdown else "text")
    result.text = content

    elements: list[Element] = []
    offset = 0
    lines = content.splitlines(keepends=True)
    index = 0
    in_fence = False
    fence_start = 0
    fence_lines: list[str] = []

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        line_start = offset
        offset += len(raw)

        if markdown and _FENCE.match(raw):
            if in_fence:
                fence_lines.append(raw)
                elements.append(
                    Element(
                        text="".join(fence_lines).strip(),
                        element_type=CODE,
                        start_offset=fence_start,
                        end_offset=offset,
                    )
                )
                in_fence = False
                fence_lines = []
            else:
                in_fence = True
                fence_start = line_start
                fence_lines = [raw]
            index += 1
            continue
        if in_fence:
            fence_lines.append(raw)
            index += 1
            continue

        if not stripped:
            index += 1
            continue

        if markdown:
            heading = _ATX_HEADING.match(stripped)
            if heading:
                elements.append(
                    Element(
                        text=heading.group(2).strip(),
                        element_type=TITLE,
                        level=len(heading.group(1)),
                        start_offset=line_start,
                        end_offset=offset,
                    )
                )
                index += 1
                continue
            # Setext headings: the underline is on the *next* line.
            if index + 1 < len(lines):
                following = lines[index + 1].strip()
                if _SETEXT_H1.match(following) or _SETEXT_H2.match(following):
                    elements.append(
                        Element(
                            text=stripped,
                            element_type=TITLE,
                            level=1 if _SETEXT_H1.match(following) else 2,
                            start_offset=line_start,
                            end_offset=offset + len(lines[index + 1]),
                        )
                    )
                    offset += len(lines[index + 1])
                    index += 2
                    continue

        if _LIST_ITEM.match(raw):
            items = [raw]
            start = line_start
            index += 1
            while index < len(lines) and (
                _LIST_ITEM.match(lines[index]) or lines[index].startswith(("  ", "\t"))
            ):
                items.append(lines[index])
                offset += len(lines[index])
                index += 1
            elements.append(
                Element(
                    text="".join(items).strip(),
                    element_type=LIST,
                    start_offset=start,
                    end_offset=offset,
                )
            )
            continue

        # A paragraph runs until a blank line.
        paragraph = [raw]
        start = line_start
        index += 1
        while index < len(lines) and lines[index].strip() and not _LIST_ITEM.match(lines[index]):
            if markdown and (
                _ATX_HEADING.match(lines[index].strip()) or _FENCE.match(lines[index])
            ):
                break
            paragraph.append(lines[index])
            offset += len(lines[index])
            index += 1
        text = "".join(paragraph).strip()
        # A lone short line with no sentence punctuation is a heading in
        # everything but syntax; treating it as prose loses the section
        # structure the chunker relies on.
        is_heading = len(paragraph) == 1 and _BARE_HEADING.match(text) and len(text.split()) <= 12
        elements.append(
            Element(
                text=text,
                element_type=TITLE if is_heading else NARRATIVE,
                level=2 if is_heading else 0,
                start_offset=start,
                end_offset=offset,
            )
        )

    if in_fence and fence_lines:
        elements.append(
            Element(
                text="".join(fence_lines).strip(),
                element_type=CODE,
                start_offset=fence_start,
                end_offset=offset,
            )
        )

    result.elements = elements
    return result
