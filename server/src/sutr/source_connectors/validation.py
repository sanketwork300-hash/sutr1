"""Validating a fetched document — once, for every connector.

Separate from `base.py` only to keep the import graph honest: this reaches into
the translation service, and the connector interface should not.
"""

from sutr.openapi.errors import OpenAPIError
from sutr.openapi.loader import parse_spec_text
from sutr.openapi.normalizer import normalize
from sutr.source_connectors.validation_result import ValidationResult


def validate_document(content: str) -> ValidationResult:
    """Parse and normalize, reporting the outcome rather than raising.

    A connector's caller wants "is this usable, and if not why" as data — a
    failed validation of a *watched* source is a routine event, not an
    exception path.
    """
    try:
        document = parse_spec_text(content)
        definition = normalize(document)
    except OpenAPIError as exc:
        return ValidationResult(
            valid=False,
            error_code=exc.code,
            message=exc.message,
            findings=list(exc.findings),
        )
    return ValidationResult(
        valid=True,
        findings=list(definition.lint_findings),
        api_title=definition.title,
        api_version=definition.version,
        operation_count=len(definition.operations),
    )
