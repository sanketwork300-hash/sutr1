"""HTML, using the standard library.

`html.parser` rather than BeautifulSoup or lxml, deliberately: this needs to
walk a tag stream and emit typed elements, which is exactly what a SAX-style
parser does, and adding a dependency to do it would be adding a dependency to
avoid writing forty lines.

Script, style and navigation content is dropped. API documentation pages are
mostly chrome by volume, and a chunk of minified JavaScript is not a business
rule — but it will happily match the patterns that look for one.
"""

from html.parser import HTMLParser

from sutr.documentation.parsing.base import (
    CODE,
    LIST,
    NARRATIVE,
    TABLE,
    TITLE,
    Element,
    ParseResult,
)

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "dd", "dt", "figcaption"}
_LIST_TAGS = {"li"}
_CODE_TAGS = {"pre", "code"}
_TABLE_CELL_TAGS = {"td", "th"}
# Content that is navigation or machinery rather than documentation.
_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "svg", "noscript", "aside", "form"}


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._stack: list[str] = []
        self._buffer: list[str] = []
        self._buffer_kind = NARRATIVE
        self._buffer_level = 0

    # ── buffering ────────────────────────────────────────────────────────────

    def _flush(self) -> None:
        text = " ".join(" ".join(self._buffer).split()).strip()
        self._buffer = []
        if not text:
            return
        start = sum(len(part) for part in self.text_parts)
        self.text_parts.append(text + "\n\n")
        self.elements.append(
            Element(
                text=text,
                element_type=self._buffer_kind,
                level=self._buffer_level,
                start_offset=start,
                end_offset=start + len(text),
            )
        )
        self._buffer_kind = NARRATIVE
        self._buffer_level = 0

    # ── tags ─────────────────────────────────────────────────────────────────

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _HEADINGS:
            self._flush()
            self._buffer_kind = TITLE
            self._buffer_level = _HEADINGS[tag]
        elif tag in _LIST_TAGS:
            self._flush()
            self._buffer_kind = LIST
        elif tag in _CODE_TAGS:
            self._flush()
            self._buffer_kind = CODE
        elif tag in _TABLE_CELL_TAGS:
            self._flush()
            self._buffer_kind = TABLE
        elif tag in _BLOCK_TAGS:
            self._flush()
        elif tag == "br":
            self._buffer.append(" ")
        self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(self._skip_depth - 1, 0)
            return
        if self._skip_depth:
            return
        if tag in _HEADINGS or tag in _LIST_TAGS or tag in _CODE_TAGS or tag in _BLOCK_TAGS:
            self._flush()
        elif tag in _TABLE_CELL_TAGS:
            self._flush()
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data):
        if self._skip_depth or not data.strip():
            return
        self._buffer.append(data)

    def close(self):
        super().close()
        self._flush()


def parse_html(content: str) -> ParseResult:
    collector = _Collector()
    collector.feed(content)
    collector.close()
    result = ParseResult(parser="html")
    result.elements = collector.elements
    result.text = "".join(collector.text_parts)
    if result.is_empty:
        result.degraded(
            "no_text",
            "The page produced no readable text. It may render its content with JavaScript, "
            "which this parser does not execute.",
        )
    return result
