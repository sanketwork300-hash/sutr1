"""Telling prose from a picture of a table.

A PDF has no idea it contains a table. `pdftotext` flattens a two-column page
into lines with the gap preserved, an architecture diagram into lines of
arrows, and a table of contents into lines of dot leaders. Every one of those
lines then looks, to a pattern that reads prose, exactly like a sentence.

The damage is specific and worth spelling out. This service's entire claim is
that every extracted fact cites the sentence it came from — so a "business
rule" whose citation points at two table cells that happened to land on the
same line is not merely noise. It is a citation that cannot be checked, which
is the one failure mode that makes all the *good* citations less believable.

Measured on a 31-page technical PDF: these checks cut extracted glossary terms
from 54 to 16 and workflows from 76 to 26, without changing what is extracted
from any prose document in the test suite.
"""

import re

# Arrows and middots are drawn between boxes; a pipe or a tab is a table.
_LAYOUT_ARTEFACTS = ("·", "•", "→", "->", "|", "\t")

# Dot leaders: a table of contents entry, not a definition.
_DOT_LEADER = re.compile(r"\.{4,}")

# Three or more spaces between two words mean columns. A single wide gap is
# the most reliable signal that a line was a layout rather than a sentence,
# which is why `chunking.split_sentences` collapses line breaks but leaves
# runs of spaces alone.
_COLUMN_GAP = re.compile(r"\S {3,}\S")

# A running page header, repeated on every page and often letter-spaced by the
# extractor ("DEVELOP ER LOW-LEVEL DESIGN"). Not a section title.
_RUNNING_HEADER = re.compile(r"^[A-Z0-9 ·—\-/&']{12,}$")

MIN_PROSE_WORDS = 4
# Below this share of lower-case letters, a run of text is a heading, an
# identifier or a code fragment rather than a sentence about something.
MIN_LOWERCASE_SHARE = 0.5


def is_layout_noise(line: str) -> bool:
    """True when this line came out of a layout rather than out of prose."""
    return (
        any(marker in line for marker in _LAYOUT_ARTEFACTS)
        or bool(_DOT_LEADER.search(line))
        or bool(_COLUMN_GAP.search(line))
    )


def is_running_header(heading: str) -> bool:
    return bool(_RUNNING_HEADER.match(heading.strip()))


def looks_like_prose(text: str) -> bool:
    """True when `text` reads as a sentence rather than as a cell.

    Not a judgement about the writing. A fragment containing an arrow came out
    of a diagram, and the words on either side of it were never a sentence.
    """
    if is_layout_noise(text):
        return False
    if len(text.split()) < MIN_PROSE_WORDS:
        return False
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return False
    lowercase = sum(character.islower() for character in letters)
    return lowercase / len(letters) > MIN_LOWERCASE_SHARE
