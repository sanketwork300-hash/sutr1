"""A caller-supplied value cannot start a new log line."""

from sutr.observability.log_safety import single_line


def test_line_breaks_are_escaped_not_obeyed():
    forged = "ok\r\nERROR admin logged in x\x85y"
    rendered = single_line(forged)
    assert "\n" not in rendered and "\r" not in rendered
    assert " " not in rendered and "\x85" not in rendered
    assert rendered.startswith("ok\\r\\nERROR admin logged in")


def test_ordinary_values_are_unchanged():
    assert single_line("github") == "github"
    assert single_line(42) == "42"
