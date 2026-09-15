"""The translation pipeline, made inspectable.

ESDS LLD §3.4 describes translation as a chain — *Specification → AST →
Semantic Graph → Normalized Graph → IR* — with *"each stage inspectable"* and
*"semantic errors halt the pipeline before IR generation"*.

Sutr's stages are not named the same, because they are not the same: there is
no separate semantic-graph pass, and inventing one to match a diagram would add
a layer that does nothing. What the LLD is actually asking for is the property,
not the vocabulary: when translation fails or produces a poor result, you
should be able to see **which stage** and **why**, rather than one error from
somewhere in the middle.

So the real stages are recorded and reported:

    parse → convert → validate → lint → resolve → normalize

Each records its outcome, its duration, and what it produced. A failure halts
the chain and the report says where. This function is the single entry point
the API and the sync service both use, so both get the same diagnostics.
"""

import time
from dataclasses import dataclass, field
from typing import Any

from sutr.openapi import fingerprint
from sutr.openapi.errors import OpenAPIError, SpecWarning
from sutr.openapi.linting import lint_document
from sutr.openapi.linting.schema_errors import schema_findings
from sutr.openapi.loader import parse_spec_text
from sutr.openapi.normalizer import ApiDefinition, normalize
from sutr.openapi.resolver import resolve_refs
from sutr.openapi.swagger2 import convert as convert_swagger2
from sutr.openapi.swagger2 import is_swagger2

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class StageResult:
    name: str
    status: str
    duration_ms: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass
class TranslationReport:
    """What happened, stage by stage."""

    stages: list[StageResult] = field(default_factory=list)
    definition: ApiDefinition | None = None
    warnings: list[SpecWarning] = field(default_factory=list)
    findings: list = field(default_factory=list)
    ir_hash: str | None = None
    ir_version: int | None = None
    content_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.definition is not None

    @property
    def failed_stage(self) -> StageResult | None:
        return next((stage for stage in self.stages if stage.status == FAILED), None)

    def as_dict(self) -> dict[str, Any]:
        failed = self.failed_stage
        return {
            "ok": self.ok,
            "stages": [stage.as_dict() for stage in self.stages],
            "failed_stage": failed.name if failed else None,
            "error_code": failed.error_code if failed else None,
            "error_message": failed.error_message if failed else None,
            "warnings": [warning.model_dump() for warning in self.warnings],
            "findings": [finding.model_dump(mode="json") for finding in self.findings],
            "ir_hash": self.ir_hash,
            "ir_version": self.ir_version,
            "content_hash": self.content_hash,
            "total_duration_ms": sum(stage.duration_ms for stage in self.stages),
        }


class _Timer:
    """Elapsed milliseconds, readable inside or after the block.

    Computed on read rather than on exit, so a stage that reports its duration
    before leaving the block gets a number rather than an AttributeError.
    """

    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        return False

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)


def translate(content: str) -> TranslationReport:
    """Run the whole chain over a raw document, recording every stage.

    Never raises for a bad document: a failure is an outcome to report, not an
    exception to handle, because the same function serves an interactive import
    and an unattended sync check.
    """
    report = TranslationReport(content_hash=fingerprint.content_hash(content))

    # ── parse ────────────────────────────────────────────────────────────────
    with _Timer() as timer:
        try:
            document = parse_spec_text(content)
        except OpenAPIError as exc:
            report.stages.append(
                StageResult(
                    "parse", FAILED, timer.ms, error_code=exc.code, error_message=exc.message
                )
            )
            return report
    declared = str(document.get("openapi") or document.get("swagger") or "")
    report.stages.append(
        StageResult(
            "parse",
            OK,
            timer.ms,
            detail={"bytes": len(content), "declared_version": declared or None},
        )
    )

    # ── convert (Swagger 2.0 → OpenAPI 3.0) ─────────────────────────────────
    converted_from: str | None = None
    with _Timer() as timer:
        if is_swagger2(document):
            converted_from = "swagger2"
            try:
                document, conversion_warnings = convert_swagger2(document)
            except OpenAPIError as exc:
                report.stages.append(
                    StageResult(
                        "convert",
                        FAILED,
                        timer.ms,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                )
                return report
            report.warnings += conversion_warnings
            report.stages.append(
                StageResult(
                    "convert",
                    OK,
                    timer.ms,
                    detail={
                        "from": "swagger2",
                        "to": document.get("openapi"),
                        "warnings": len(conversion_warnings),
                    },
                )
            )
        else:
            report.stages.append(
                StageResult(
                    "convert", SKIPPED, 0, detail={"reason": "The document is already OpenAPI 3.x."}
                )
            )

    version = str(document.get("openapi") or "3.0")

    # ── validate ─────────────────────────────────────────────────────────────
    with _Timer() as timer:
        violations = schema_findings(document, version)
    if violations:
        report.findings += violations
        report.stages.append(
            StageResult(
                "validate",
                FAILED,
                timer.ms,
                detail={"violations": len(violations)},
                error_code="invalid_spec",
                error_message=violations[0].message,
            )
        )
        # Semantic errors halt the pipeline before IR generation (LLD §3.4).
        return report
    report.stages.append(StageResult("validate", OK, timer.ms))

    # ── lint ─────────────────────────────────────────────────────────────────
    with _Timer() as timer:
        lint = lint_document(document)
    report.findings += lint.findings
    report.stages.append(StageResult("lint", OK, timer.ms, detail={"counts": lint.counts()}))

    # ── resolve ($refs) ──────────────────────────────────────────────────────
    with _Timer() as timer:
        try:
            resolve_refs(document)
        except OpenAPIError as exc:
            report.stages.append(
                StageResult(
                    "resolve",
                    FAILED,
                    timer.ms,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            )
            return report
    report.stages.append(StageResult("resolve", OK, timer.ms))

    # ── normalize (→ IR) ─────────────────────────────────────────────────────
    with _Timer() as timer:
        try:
            definition = normalize(document)
        except OpenAPIError as exc:
            report.stages.append(
                StageResult(
                    "normalize",
                    FAILED,
                    timer.ms,
                    error_code=exc.code,
                    error_message=exc.message,
                )
            )
            return report
    if converted_from:
        # `normalize` only ever sees OpenAPI 3.x — the conversion happened
        # above — so the original dialect is known here and nowhere else.
        definition.source_dialect = converted_from
    report.definition = definition
    report.warnings += definition.warnings
    report.ir_hash = fingerprint.fingerprint(definition)
    report.ir_version = definition.ir_version
    report.stages.append(
        StageResult(
            "normalize",
            OK,
            timer.ms,
            detail={
                "operations": len(definition.operations),
                "servers": len(definition.servers),
                "security_schemes": len(definition.security_schemes),
                "source_dialect": definition.source_dialect,
                "ir_hash": report.ir_hash,
            },
        )
    )
    return report
