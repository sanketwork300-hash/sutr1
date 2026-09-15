"""Semantic chunking: group by section, never by a fixed window.

ESDS LLD §3.5 puts *"chunk semantically"* between parsing and extraction, and
the reason is concrete rather than stylistic. A fixed 500-token window cuts
sentences in half, and half a sentence is worse than useless to a rule
extractor: *"Refunds are allowed only within"* looks like a rule and is not
one.

So chunks follow the document's own structure, the way
`Unstructured-IO/unstructured` does it: partition into typed elements, then
group consecutive elements under the heading they sit beneath. A chunk is
therefore a *section*, and it carries the heading path — "Refunds >
Eligibility" — which makes every citation readable and gives retrieval
something to show.

Two bounds keep it practical:

- a chunk that grows past `MAX_CHUNK_CHARS` is split **at a sentence
  boundary**, never mid-sentence;
- a section too small to stand alone is merged forward, because a chunk
  containing only a heading tells a reader nothing.
"""

import re
from dataclasses import dataclass, field

from sutr.documentation.parsing.base import CODE, TITLE, Element

# Large enough to hold a whole rule with its surrounding qualification; small
# enough that a retrieval hit points at a paragraph rather than a chapter.
MAX_CHUNK_CHARS = 2000
# Below this a chunk is merged forward: on its own it is a fragment.
MIN_CHUNK_CHARS = 80
# Roughly four characters per token. Used only for reporting a size, never for
# a decision, so the imprecision costs nothing.
CHARS_PER_TOKEN = 4

# Sentence boundaries. Deliberately conservative: an abbreviation followed by a
# capital ("e.g. Refunds") should not split a sentence, so a boundary needs
# whitespace after the punctuation and a following capital or digit.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


@dataclass
class Chunk:
    """One semantically whole piece of a document."""

    text: str
    position: int = 0
    section_path: str = ""
    element_type: str = "narrative"
    start_offset: int = 0
    end_offset: int = 0
    page: int | None = None

    @property
    def token_estimate(self) -> int:
        return max(len(self.text) // CHARS_PER_TOKEN, 1)


@dataclass
class _Section:
    heading_path: list[str] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)

    @property
    def path(self) -> str:
        return " > ".join(self.heading_path)


# Only line breaks are collapsed, and deliberately not runs of spaces: a wide
# gap inside a line is how `pdftotext` renders two columns, and it is the one
# signal that tells the extractors a line was a table rather than a sentence.
_LINE_BREAK = re.compile(r"[ \t]*\n[ \t]*")


def _join_lines(text: str) -> str:
    return _LINE_BREAK.sub(" ", text).strip()


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences, keeping their punctuation.

    Line breaks *inside* a sentence are collapsed to single spaces. Hard
    wrapping is a rendering artifact of the source — PDFs and plain text are
    full of it — and a sentence that arrives as two lines is still one
    sentence. Leaving the break in means every pattern in the extractors has
    to anticipate a newline at an arbitrary word boundary, which is how a
    prohibition like "Refunds are not permitted for digital goods that have\n
    been downloaded." silently stops being found.

    The returned text no longer matches the chunk character-for-character, so
    `locate()` is what maps it back to offsets.
    """
    parts = [_join_lines(part) for part in _SENTENCE_END.split(text) if part.strip()]
    return [part for part in parts if part] or ([_join_lines(text)] if text.strip() else [])


def locate(haystack: str, needle: str) -> tuple[int, int]:
    """Find `needle` in `haystack`, tolerant of differing whitespace.

    Citations must point at the passage in the original text, but the text an
    extractor matched has had its line breaks flattened. Comparing token by
    token with `\\s+` between them finds the original span; an exact search is
    tried first because it is the common case and much cheaper.
    """
    if not needle:
        return 0, 0
    position = haystack.find(needle)
    if position >= 0:
        return position, position + len(needle)
    pattern = r"\s+".join(re.escape(token) for token in needle.split())
    match = re.search(pattern, haystack)
    if match:
        return match.start(), match.end()
    return 0, len(haystack)


def _sections(elements: list[Element]) -> list[_Section]:
    """Group elements under the heading they sit beneath.

    Headings nest: an `h3` after an `h2` extends the path rather than replacing
    it, so "Refunds > Eligibility" is preserved instead of losing "Refunds".
    """
    sections: list[_Section] = []
    heading_path: list[str] = []
    current = _Section(heading_path=[])

    for element in elements:
        if element.element_type == TITLE:
            if current.elements:
                sections.append(current)
            level = element.level or 1
            # A deeper heading extends the path; a shallower one truncates it.
            heading_path = heading_path[: max(level - 1, 0)]
            heading_path.append(element.text.strip())
            current = _Section(heading_path=list(heading_path))
        else:
            current.elements.append(element)

    if current.elements:
        sections.append(current)
    return sections


def _split_long(text: str, limit: int) -> list[str]:
    """Break an over-long block at sentence boundaries."""
    sentences = split_sentences(text)
    pieces: list[str] = []
    buffer: list[str] = []
    length = 0
    for sentence in sentences:
        if length and length + len(sentence) + 1 > limit:
            pieces.append(" ".join(buffer))
            buffer, length = [], 0
        buffer.append(sentence)
        length += len(sentence) + 1
    if buffer:
        pieces.append(" ".join(buffer))

    # A single sentence longer than the limit is left whole: cutting it would
    # destroy exactly the thing chunking exists to preserve.
    return pieces or [text]


def chunk_elements(elements: list[Element], *, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """Group parsed elements into section-shaped chunks."""
    chunks: list[Chunk] = []
    position = 0

    for section in _sections(elements):
        buffer: list[Element] = []
        buffer_length = 0

        def flush() -> None:
            nonlocal buffer, buffer_length, position
            if not buffer:
                return
            text = "\n\n".join(element.text.strip() for element in buffer if element.text.strip())
            if not text:
                buffer, buffer_length = [], 0
                return
            pieces = _split_long(text, max_chars) if len(text) > max_chars else [text]
            for piece in pieces:
                chunks.append(
                    Chunk(
                        text=piece,
                        position=position,
                        section_path=section.path,
                        element_type=buffer[0].element_type,
                        start_offset=buffer[0].start_offset,
                        end_offset=buffer[-1].end_offset,
                        page=buffer[0].page,
                    )
                )
                position += 1
            buffer, buffer_length = [], 0

        for element in section.elements:
            # Code blocks stand alone: mixing a JSON sample into a prose chunk
            # gives the rule extractor a page of braces to match against.
            if element.element_type == CODE:
                flush()
                if element.text.strip():
                    chunks.append(
                        Chunk(
                            text=element.text.strip(),
                            position=position,
                            section_path=section.path,
                            element_type=CODE,
                            start_offset=element.start_offset,
                            end_offset=element.end_offset,
                            page=element.page,
                        )
                    )
                    position += 1
                continue

            if buffer_length and buffer_length + len(element.text) > max_chars:
                flush()
            buffer.append(element)
            buffer_length += len(element.text) + 2

        flush()

    return _merge_fragments(chunks)


def _merge_fragments(chunks: list[Chunk]) -> list[Chunk]:
    """Fold undersized chunks into the next one in the same section.

    A chunk holding one short line is a fragment; on its own it is neither
    retrievable nor extractable.
    """
    if not chunks:
        return chunks
    merged: list[Chunk] = []
    for chunk in chunks:
        if (
            merged
            and len(merged[-1].text) < MIN_CHUNK_CHARS
            and merged[-1].section_path == chunk.section_path
            and merged[-1].element_type != CODE
            and chunk.element_type != CODE
            and len(merged[-1].text) + len(chunk.text) <= MAX_CHUNK_CHARS
        ):
            previous = merged[-1]
            previous.text = f"{previous.text}\n\n{chunk.text}"
            previous.end_offset = chunk.end_offset
            continue
        merged.append(chunk)

    for index, chunk in enumerate(merged):
        chunk.position = index
    return merged
