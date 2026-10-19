"""What validating a fetched document concluded.

Its own module so that `base.py` (which runs validation) and `validation.py`
(which produces the result) can both import it without importing each other.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationResult:
    """Whether a fetched document can be translated, and what is wrong if not."""

    valid: bool
    error_code: str | None = None
    message: str = ""
    # Findings from the lint layer, when the document parsed at all.
    findings: list = field(default_factory=list)
    api_title: str | None = None
    api_version: str | None = None
    operation_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "error_code": self.error_code,
            "message": self.message,
            "findings": [finding.model_dump(mode="json") for finding in self.findings],
            "api_title": self.api_title,
            "api_version": self.api_version,
            "operation_count": self.operation_count,
        }
