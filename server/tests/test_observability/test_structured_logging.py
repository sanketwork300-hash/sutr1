"""Structured JSON logging (ESDS LLD §5.3, build prompt §12).

Two properties matter and both are pinned here:
1. Every line carries the mandated field set, populated from context rather
   than from what the call site happened to remember to pass.
2. Credentials never reach the log stream, even when a caller logs them
   directly — redaction lives in the formatter, not at call sites.
"""

import json
import logging

import pytest

from sutr.observability import log_context
from sutr.observability.log_context import LOG_FIELDS
from sutr.observability.log_format import JsonFormatter, TextFormatter
from sutr.observability.log_setup import configure_logging
from sutr.request_context import set_correlation_id, set_request_id


@pytest.fixture(autouse=True)
def clean_context():
    token = log_context.clear()
    yield
    log_context.reset(token)


def _record(message: str = "hello", **extra) -> logging.LogRecord:
    record = logging.LogRecord("sutr.test", logging.INFO, __file__, 10, message, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _emit(message: str = "hello", **extra) -> dict:
    return json.loads(JsonFormatter().format(_record(message, **extra)))


def test_every_mandated_field_is_present_even_when_unknown():
    payload = _emit()
    for field in LOG_FIELDS:
        assert field in payload, f"{field} missing from the log line"
    assert payload["level"] == "INFO"
    assert payload["service"] == "sutr"
    assert payload["logger"] == "sutr.test"
    assert payload["message"] == "hello"
    assert payload["timestamp"].endswith("+00:00")


def test_bound_context_populates_the_fields():
    log_context.bind(
        tenant_id="org-1",
        agent_id="api_key:abc",
        provider_id="stripe",
        tool_id="create_charge",
        runtime_id="rt-9",
        status="executed",
        duration_ms=42,
        region="ap-south-1",
    )
    payload = _emit()
    assert payload["tenant_id"] == "org-1"
    assert payload["agent_id"] == "api_key:abc"
    assert payload["provider_id"] == "stripe"
    assert payload["tool_id"] == "create_charge"
    assert payload["runtime_id"] == "rt-9"
    assert payload["status"] == "executed"
    assert payload["duration_ms"] == 42
    assert payload["region"] == "ap-south-1"


def test_extra_on_the_record_overrides_bound_context():
    log_context.bind(tool_id="from_context")
    assert _emit(tool_id="from_call_site")["tool_id"] == "from_call_site"


def test_correlation_id_falls_back_to_request_context():
    set_correlation_id("corr-123")
    set_request_id("req-456")
    payload = _emit()
    assert payload["correlation_id"] == "corr-123"
    assert payload["request_id"] == "req-456"


def test_unknown_field_names_are_rejected_rather_than_silently_dropped():
    with pytest.raises(ValueError, match="unknown log field"):
        log_context.bind(not_a_field="x")


def test_bind_ignores_none_so_optional_ids_can_be_passed_unconditionally():
    log_context.bind(tool_id="t", agent_id=None)
    payload = _emit()
    assert payload["tool_id"] == "t"
    assert payload["agent_id"] is None


def test_bound_context_manager_restores_previous_values():
    log_context.bind(tool_id="outer")
    with log_context.bound(tool_id="inner"):
        assert _emit()["tool_id"] == "inner"
    assert _emit()["tool_id"] == "outer"


# ── Redaction ────────────────────────────────────────────────────────────────


def test_a_bearer_token_in_the_message_is_redacted():
    payload = _emit("calling upstream with Bearer sk_live_ABCDEFGHIJKLMNOPQRSTUV")
    assert "sk_live_ABCDEFGHIJKLMNOPQRSTUV" not in payload["message"]
    assert "[REDACTED]" in payload["message"]


def test_a_jwt_in_the_message_is_redacted():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    payload = _emit(f"token was {jwt}")
    assert jwt not in payload["message"]


def test_credential_shaped_extra_keys_are_redacted():
    payload = _emit("saved", context_note={"api_key": "secret-value", "user": "ada"})
    dumped = json.dumps(payload)
    assert "secret-value" not in dumped
    assert "ada" in dumped


def test_exception_text_is_redacted_too():
    try:
        raise RuntimeError("upstream rejected Bearer sk_live_ABCDEFGHIJKLMNOPQRSTUV")
    except RuntimeError:
        record = _record("failed")
        import sys

        record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
    assert "sk_live_ABCDEFGHIJKLMNOPQRSTUV" not in payload["exception"]


# ── Setup ────────────────────────────────────────────────────────────────────


def test_configure_logging_is_idempotent_and_swaps_the_formatter():
    root = logging.getLogger()
    before = len(root.handlers)
    configure_logging(level="INFO", log_format="json")
    configure_logging(level="INFO", log_format="text")
    after = [h for h in root.handlers if getattr(h, "_sutr_handler", False)]
    assert len(after) == 1, "re-configuring must not stack handlers"
    assert isinstance(after[0].formatter, TextFormatter)
    configure_logging(level="INFO", log_format="json")
    assert isinstance(after[0].formatter, JsonFormatter)
    assert len(root.handlers) <= before + 1


def test_an_unknown_format_falls_back_to_json_rather_than_crashing():
    configure_logging(level="INFO", log_format="yaml-please")
    handler = next(h for h in logging.getLogger().handlers if getattr(h, "_sutr_handler", False))
    assert isinstance(handler.formatter, JsonFormatter)


def test_text_format_still_carries_the_correlation_id():
    set_correlation_id("corr-text")
    assert "correlation_id=corr-text" in TextFormatter().format(_record())
