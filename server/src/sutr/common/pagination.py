"""Cursor pagination for the `/v1` surface (ESDS LLD §5.5).

Offset pagination is wrong for a growing table: rows inserted between two
requests shift every later page, so a client walking pages either sees a row
twice or misses it entirely. A cursor names the last row seen, so the walk is
stable no matter what is inserted behind it.

The cursor is opaque on purpose — base64 of a compact JSON tuple. Clients must
treat it as a token they received and hand back, never as something to
construct: the encoding is free to change, and anything a client parses out of
it becomes a compatibility obligation nobody agreed to.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from sutr.common.errors import InvalidRequestError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def encode_cursor(values: dict[str, Any]) -> str:
    """Encode the position of the last row on a page."""
    raw = json.dumps(values, separators=(",", ":"), sort_keys=True, default=str).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    """Decode a cursor a client handed back.

    A malformed cursor is the client's problem, not a server error: it gets a
    400 naming the parameter rather than a 500 naming nothing.
    """
    if not cursor:
        return {}
    padding = "=" * (-len(cursor) % 4)
    try:
        decoded = base64.urlsafe_b64decode(cursor + padding)
        values = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise InvalidRequestError(
            "The `cursor` parameter is not a cursor this API issued.",
            details={"parameter": "cursor"},
        )
    if not isinstance(values, dict):
        raise InvalidRequestError(
            "The `cursor` parameter is not a cursor this API issued.",
            details={"parameter": "cursor"},
        )
    return values


@dataclass(frozen=True)
class PageRequest:
    """What the caller asked for."""

    limit: int = DEFAULT_LIMIT
    cursor: dict[str, Any] | None = None

    @classmethod
    def parse(cls, limit: int | None = None, cursor: str | None = None) -> "PageRequest":
        effective = DEFAULT_LIMIT if limit is None else limit
        if effective < 1:
            raise InvalidRequestError("`limit` must be at least 1.", details={"parameter": "limit"})
        if effective > MAX_LIMIT:
            raise InvalidRequestError(
                f"`limit` may not exceed {MAX_LIMIT}.",
                details={"parameter": "limit", "maximum": MAX_LIMIT},
            )
        return cls(limit=effective, cursor=decode_cursor(cursor) or None)

    @property
    def fetch_limit(self) -> int:
        """One more than asked for.

        Fetching `limit + 1` is how the page knows whether another page exists
        without a second `COUNT(*)` over a table that may be large.
        """
        return self.limit + 1


@dataclass
class Page:
    """What the caller gets back."""

    items: list
    next_cursor: str | None = None

    @classmethod
    def build(cls, rows: list, request: PageRequest, cursor_for) -> "Page":
        """Trim an over-fetched row set into a page and derive its cursor.

        `cursor_for(row)` returns the dict identifying that row's position.
        """
        has_more = len(rows) > request.limit
        items = rows[: request.limit]
        next_cursor = encode_cursor(cursor_for(items[-1])) if has_more and items else None
        return cls(items=items, next_cursor=next_cursor)
