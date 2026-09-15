"""The success response envelope for the `/v1` surface.

Named `responses` rather than `envelope` on purpose: the package exports a
function called `envelope`, and a module of the same name would shadow it for
anyone importing the package — exactly the kind of subtlety that wastes an
afternoon. The LLD's own word for this shared library's job is "responses"
(§2.6).

ADR-004: `/api/...` keeps its bare-JSON bodies forever, because the published
CLI and both SDKs read them. `/v1/...` is the new surface and carries an
envelope, so a client can find the payload, the pagination cursor, and the
correlation id in the same place on every response.

    {"data": <the payload>,
     "meta": {"request_id": ..., "correlation_id": ..., "next_cursor": ...}}

`data` is always present, even when null. `meta` is always an object, even
when empty. Neither is ever omitted, because "absent" and "empty" being
different is exactly the kind of subtlety that costs a client an afternoon.
"""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from sutr.request_context import get_correlation_id, get_request_id

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: dict[str, Any] = {}


def meta_for_request(**extra: Any) -> dict[str, Any]:
    """Correlation metadata for the request being handled, plus any extras.

    Read from the request context rather than passed in, so a handler cannot
    forget it and a background stage gets the same ids for free.
    """
    meta: dict[str, Any] = {}
    request_id = get_request_id()
    if request_id:
        meta["request_id"] = request_id
    correlation_id = get_correlation_id()
    if correlation_id:
        meta["correlation_id"] = correlation_id
    for key, value in extra.items():
        if value is not None:
            meta[key] = value
    return meta


def envelope(data: Any, **meta: Any) -> dict:
    """Wrap a payload for the `/v1` surface."""
    return {"data": data, "meta": meta_for_request(**meta)}
