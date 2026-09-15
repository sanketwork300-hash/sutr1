"""Spectral-style OpenAPI linting (build prompt §16, ESDS LLD §3.4).

Structural validity against the official meta-schema stays the hard gate
(`openapi/normalizer.py`); linting is the advisory layer on top of it. Its job
is to answer "how good a tool will this specification produce?" and to answer
it *completely* — all findings, not the first one that trips.

Rule ids follow Spectral's `oas` ruleset where the same check exists, so a
finding is recognisable to anyone who already lints with Spectral; rules with
no Spectral equivalent carry a `sutr-` prefix. The rules are reimplemented
natively rather than shelling out to Spectral, which is a Node toolchain
(ADR-013 in docs/ARCHITECTURE_DECISIONS.md).
"""

# Importing the rule modules is what registers them.
from sutr.openapi.linting import (  # noqa: F401
    rules_document,
    rules_operations,
    rules_schema,
    rules_security,
)
from sutr.openapi.linting.finding import LintFinding, Severity
from sutr.openapi.linting.registry import LintReport, all_rules, run_rules


def lint_document(document: dict) -> LintReport:
    """Run every applicable rule over a parsed OpenAPI document.

    `document` is the *original* parsed document, before `$ref` resolution:
    findings point at the file the user wrote, not at a rewritten copy.
    """
    version = str(document.get("openapi") or document.get("swagger") or "3.0")
    return LintReport(findings=run_rules(document, version))


__all__ = [
    "LintFinding",
    "LintReport",
    "Severity",
    "all_rules",
    "lint_document",
]
