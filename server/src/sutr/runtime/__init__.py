"""Tool execution runtimes.

ADR-010 unifies Sutr's two execution modes behind one definition of what a
request *is*:

- ``HOSTED``     — the gateway proxies a compiled tool dynamically, applying
                   policy, approvals, metering, and logging on the way.
- ``STANDALONE`` — a generated package runs the same tools on the user's own
                   infrastructure, independent of the platform.

Both build their requests with `runtime.request_builder`, which is embedded
verbatim into generated packages so the two can never drift.
"""

from enum import Enum


class RuntimeMode(str, Enum):
    HOSTED = "HOSTED"
    STANDALONE = "STANDALONE"


__all__ = ["RuntimeMode"]
