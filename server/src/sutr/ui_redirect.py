"""Where the server may send a browser: into its own UI, and nowhere else.

Several flows end in a redirect the server builds (a connected-account
authorization, Google sign-in), and some carry a "take me back to X" value a
client supplied. Every such URL is built here, so the destination is always
`settings.ui_base_url` followed by a path this module has vetted. A target that
could steer the browser anywhere else is replaced with a fallback, never
repaired.
"""

from urllib.parse import urlencode, urlsplit

from sutr.config import settings


def is_safe_ui_path(target: str | None) -> bool:
    """Whether `target` is a plain absolute path inside the UI.

    A plain path starts with exactly one `/` and names no scheme or host.
    Browsers read a backslash as a slash and silently drop tabs and newlines,
    so `/\\evil.com` and `/<TAB>/evil.com` both arrive as the protocol-relative
    `//evil.com`; those characters disqualify a target rather than being
    stripped from it.
    """
    if not target or "\\" in target:
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in target):
        return False
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return False
    return target.startswith("/") and not target.startswith("//")


def safe_ui_path(target: str | None, *, fallback: str) -> str:
    """`target` when it is a plain path inside the UI, otherwise `fallback`."""
    return target if target is not None and is_safe_ui_path(target) else fallback


def ui_url(path: str, *, query: dict[str, str] | None = None, fragment: str = "") -> str:
    """An absolute URL into the UI.

    `path` must be a literal or the output of `safe_ui_path`. Query values are
    encoded here, so a caller cannot add parameters or end the query early.
    """
    url = f"{settings.ui_base_url.rstrip('/')}{path}"
    if query:
        url += ("&" if "?" in path else "?") + urlencode(query)
    if fragment:
        url += f"#{fragment}"
    return url
