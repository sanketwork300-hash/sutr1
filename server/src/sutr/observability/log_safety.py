"""Making a caller-supplied value safe to put in a log line.

The JSON formatter escapes line breaks on its own, but the text formatter used
in development does not, and neither can tell a forged line from a real one
once the record has been written. A value that came from outside (a provider
error string, an email address, a tool name) goes through `single_line` before
it is logged, so it cannot end the current entry and start a convincing new one.
"""

# Characters other than CR and LF that some log viewers render as a line break.
_OTHER_LINE_BREAKS = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


def single_line(value: object) -> str:
    """`value` as text with every line break shown as an escape, not obeyed."""
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    for char in _OTHER_LINE_BREAKS:
        # `ascii()` spells the character as its escape, e.g. `\u2028`.
        text = text.replace(char, ascii(char)[1:-1])
    return text
