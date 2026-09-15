"""Source connectors: where API definitions live (ESDS LLD §3.3).

    connect · discover · fetch · validate · watch · detect_drift · disconnect

Every connector implements all seven, and every one of them converges on the
same normalization pipeline — so the compiler never learns where a document
came from, which is the property that keeps one translation path instead of
seven.
"""

from sutr.source_connectors.base import (
    WATCH_NONE,
    WATCH_POLL,
    WATCH_WEBHOOK,
    ConfigField,
    ConnectionResult,
    ConnectorError,
    Discovered,
    FetchResult,
    Provenance,
    SourceConnector,
    ValidationResult,
    WatchPlan,
)
from sutr.source_connectors.registry import all_connectors, describe_all, get, require

__all__ = [
    "WATCH_NONE",
    "WATCH_POLL",
    "WATCH_WEBHOOK",
    "ConfigField",
    "ConnectionResult",
    "ConnectorError",
    "Discovered",
    "FetchResult",
    "Provenance",
    "SourceConnector",
    "ValidationResult",
    "WatchPlan",
    "all_connectors",
    "describe_all",
    "get",
    "require",
]
