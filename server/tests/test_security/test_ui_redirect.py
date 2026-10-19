"""Server-built redirects only ever land inside the configured UI."""

import pytest

from sutr.config import settings
from sutr.ui_redirect import is_safe_ui_path, safe_ui_path, ui_url


@pytest.mark.parametrize(
    "target", ["/app", "/integrations/mcp-builder", "/x?tab=1", "/a/b#section"]
)
def test_plain_paths_are_kept(target):
    assert is_safe_ui_path(target)
    assert safe_ui_path(target, fallback="/home") == target


@pytest.mark.parametrize(
    "target",
    [
        None,
        "",
        "app",
        "//evil.example.com",
        "/\\evil.example.com",
        "/\t/evil.example.com",
        "/\n/evil.example.com",
        "https://evil.example.com/steal",
        "javascript:alert(1)",
        "\\\\evil.example.com",
    ],
)
def test_anything_that_could_leave_the_ui_is_replaced(target):
    assert not is_safe_ui_path(target)
    assert safe_ui_path(target, fallback="/home") == "/home"


def test_query_values_cannot_add_parameters(monkeypatch):
    monkeypatch.setattr(settings, "ui_base_url", "https://ui.example/")
    url = ui_url("/login", query={"google_error": "x&next=https://evil.example.com#y"})
    assert url.startswith("https://ui.example/login?google_error=")
    assert "&next=" not in url
    assert "#" not in url
