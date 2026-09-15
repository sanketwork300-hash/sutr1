"""Structured JSON log formatting (ESDS LLD §5.3, build prompt §12).

One line per record, one JSON object per line, with a fixed field set so a log
pipeline (Loki, OpenSearch, Fluent Bit) can index without per-service rules.

Redaction is applied *here*, in the formatter, rather than at call sites. A
rule that every call site must remember is a rule that will be broken; doing it
once at the boundary means a credential cannot reach the log stream even if
someone logs an argument dict by mistake. The rules are the same ones already
used for stored tool-call logs (`services/redaction.py`), so "what counts as a
secret" has exactly one definition in the codebase.
"""

import json
import logging
from datetime import datetime, timezone

from sutr.config import settings
from sutr.observability.log_context import LOG_FIELDS, current_fields
from sutr.observability.propagation import current_span_id, current_trace_id
from sutr.request_context import get_correlation_id, get_request_id
from sutr.services.redaction import _redact_value

# Attributes the stdlib puts on every record; anything else a caller passed via
# `extra=` is user data and is emitted (redacted) under "context".
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "exc_text",
}


class JsonFormatter(logging.Formatter):
    """Format a record as a single-line JSON object."""

    def __init__(self, service: str = "sutr"):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": _redact_value(record.getMessage()),
        }

        # The LLD field set is always present so queries never have to cope
        # with a missing key; unknown values are explicit nulls.
        bound = current_fields()
        extras = {
            key: value for key, value in record.__dict__.items() if key not in _STANDARD_ATTRS
        }
        for field in LOG_FIELDS:
            payload[field] = extras.pop(field, None) or bound.get(field)
        if payload["correlation_id"] is None:
            payload["correlation_id"] = get_correlation_id()
        # The region is a property of the process, not of the call, so it comes
        # from configuration rather than from something a call site remembered
        # to bind. Still null when unset: an unset region is unknown, and
        # guessing one would put a wrong answer on every line.
        if payload["region"] is None:
            payload["region"] = settings.region or None

        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id

        # Present only while a trace is actually recording. A line that claims
        # a trace id no backend holds sends the reader somewhere empty, so the
        # key is absent rather than null when tracing is off.
        trace_id = current_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = current_span_id()

        if extras:
            payload["context"] = _redact_value(extras)

        if record.exc_info:
            payload["exception"] = _redact_value(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable output for local development.

    Carries the correlation id so a developer can still follow one request
    through the log, but does not attempt the full field set.
    """

    def __init__(self):
        super().__init__(fmt="%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        correlation_id = current_fields().get("correlation_id") or get_correlation_id()
        return f"{base} [correlation_id={correlation_id}]" if correlation_id else base
