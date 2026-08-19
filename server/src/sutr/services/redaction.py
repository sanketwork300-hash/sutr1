"""Best-effort secret redaction for stored tool-call logs.

Applied to LogEntry.args_json / result_json only. ToolApprovalRequest.args_json
is deliberately NOT redacted — the approved arguments are re-executed verbatim
by the await-approval flow, and redacting them would corrupt the call.

Two layers:
- key-based: any JSON object key that looks credential-like has its value
  replaced entirely.
- value-based: long bearer-token-shaped strings inside otherwise innocent
  values (e.g. an Authorization header echoed into an error message).
"""

import json
import re

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(.*[_\-])?("
    r"pass(word|phrase)?|secret|token|api[_\-]?key|apikey|authorization|auth|"
    r"credential[s]?|private[_\-]?key|access[_\-]?key|client[_\-]?secret|"
    r"session[_\-]?id|cookie|bearer"
    r")([_\-].*)?$"
)

# JWT-shaped (three base64url segments) or explicit bearer prefix.
_SENSITIVE_VALUE_RE = re.compile(
    r"(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,})"
    r"|((?i:bearer)\s+[A-Za-z0-9._\-]{16,})"
)


def _redact_value(value):
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and _SENSITIVE_KEY_RE.match(key)
                else _redact_value(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE_RE.sub(REDACTED, value)
    return value


def redact_args(args: dict) -> str:
    """JSON-encode tool args with credential-shaped content removed."""
    return json.dumps(_redact_value(args))


def redact_result(result: dict) -> str:
    """JSON-encode a tool result with credential-shaped content removed."""
    return json.dumps(_redact_value(result))
