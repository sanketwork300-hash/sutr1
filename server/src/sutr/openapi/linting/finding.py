"""The shape of one lint finding.

Every field required by build prompt §16 is mandatory: a finding without a
rule id cannot be suppressed, one without a JSON pointer cannot be located,
and one without remediation tells the user they have a problem without telling
them what to do about it.
"""

from enum import Enum

from pydantic import BaseModel


class Severity(str, Enum):
    """Three levels, per build prompt §16.

    ERROR   — the specification is wrong or will produce a broken tool.
    WARNING — the specification is legal but will produce a worse tool.
    INFO    — a best-practice observation with no effect on generation.
    """

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class LintFinding(BaseModel):
    rule_id: str
    severity: Severity
    message: str
    # Human-readable place, e.g. "GET /users" or "components.securitySchemes.apiKey".
    location: str
    # RFC 6901 pointer into the *original* document, so an editor can jump to it.
    json_pointer: str
    # Anchor in this project's rule reference (docs/pages/openapi-linting.md).
    documentation: str
    remediation: str


def pointer(*segments) -> str:
    """Build an RFC 6901 JSON pointer from raw path segments."""
    escaped = [str(s).replace("~", "~0").replace("/", "~1") for s in segments]
    return "/" + "/".join(escaped) if escaped else ""
