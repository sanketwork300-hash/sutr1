"""Structural validation findings — every error, not just the first.

`openapi_spec_validator.validate()` raises on the first problem, which makes
fixing a large specification a game of whack-a-mole. `iter_errors()` yields
them all, and this module turns each into the same `LintFinding` shape the
rule engine produces, so a caller has one list to render (build prompt §16).

The rule id is Spectral's `oas3-schema`, which is the rule Spectral uses for
exactly this check.
"""

from openapi_spec_validator import (
    OpenAPIV30SpecValidator,
    OpenAPIV31SpecValidator,
)

from sutr.openapi.linting.finding import LintFinding, Severity, pointer

RULE_ID = "oas3-schema"
_DOC = f"/openapi-linting#{RULE_ID}"


def _validator_for(version: str):
    return OpenAPIV31SpecValidator if version.startswith("3.1") else OpenAPIV30SpecValidator


def schema_findings(document: dict, version: str, limit: int = 200) -> list[LintFinding]:
    """Every meta-schema violation in the document, as findings.

    `limit` bounds the work a pathological document can cause; when it is hit
    the last finding says so rather than silently truncating.
    """
    validator = _validator_for(version)(document)
    findings: list[LintFinding] = []
    truncated = False
    for error in validator.iter_errors():
        if len(findings) >= limit:
            truncated = True
            break
        path = list(getattr(error, "absolute_path", []) or [])
        message = str(error).split("\n")[0][:500]
        findings.append(
            LintFinding(
                rule_id=RULE_ID,
                severity=Severity.ERROR,
                message=message,
                location="/".join(str(p) for p in path) or "(root)",
                json_pointer=pointer(*path),
                documentation=_DOC,
                remediation=(
                    "Correct the document so it satisfies the OpenAPI "
                    f"{version} schema at this location."
                ),
            )
        )
    if truncated:
        findings.append(
            LintFinding(
                rule_id=RULE_ID,
                severity=Severity.ERROR,
                message=(
                    f"More than {limit} schema violations; only the first {limit} are listed. "
                    "Fix these and re-import to see the rest."
                ),
                location="(root)",
                json_pointer="",
                documentation=_DOC,
                remediation="Fix the listed violations and validate again.",
            )
        )
    return findings
