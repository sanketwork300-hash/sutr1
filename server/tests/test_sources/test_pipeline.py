"""The translation pipeline and IR identity (LLD §3.4, build prompt §14).

Build prompt §14 requires the IR to be *deterministic, versioned, inspectable,
reproducible* — "same specification + same compiler version must produce the
same IR". Determinism that is intended but never checked is determinism that
will quietly stop being true, so it is asserted here.
"""

import json

from sutr.openapi import fingerprint
from sutr.openapi.fingerprint import IR_VERSION
from sutr.openapi.normalizer import normalize
from sutr.openapi.pipeline import OK, SKIPPED, translate

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.0.0", "description": "Pets."},
    "servers": [{"url": "https://api.example.com"}],
    "components": {
        "schemas": {"Pet": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "securitySchemes": {"k": {"type": "apiKey", "in": "header", "name": "X-Key"}},
    },
    "security": [{"k": []}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets.",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}
                        },
                    }
                },
            }
        }
    },
}


def _text() -> str:
    return json.dumps(SPEC)


# ── Determinism ──────────────────────────────────────────────────────────────


def test_the_same_specification_produces_the_same_ir():
    first = normalize(json.loads(_text()))
    second = normalize(json.loads(_text()))
    assert fingerprint.fingerprint(first) == fingerprint.fingerprint(second)
    assert fingerprint.canonical_json(first) == fingerprint.canonical_json(second)


def test_the_fingerprint_survives_a_full_round_trip_through_json():
    from sutr.openapi.normalizer import ApiDefinition

    original = normalize(json.loads(_text()))
    restored = ApiDefinition.model_validate_json(original.model_dump_json())
    assert fingerprint.fingerprint(restored) == fingerprint.fingerprint(original)


def test_reformatting_the_document_does_not_change_the_ir():
    """A file can be reformatted without the API changing, and drift detection
    depends on telling those apart."""
    compact = normalize(json.loads(json.dumps(SPEC)))
    pretty = normalize(json.loads(json.dumps(SPEC, indent=4, sort_keys=True)))
    assert fingerprint.fingerprint(compact) == fingerprint.fingerprint(pretty)
    assert fingerprint.content_hash(json.dumps(SPEC)) != fingerprint.content_hash(
        json.dumps(SPEC, indent=4, sort_keys=True)
    ), "the raw document hash must still differ"


def test_a_real_change_changes_the_fingerprint():
    changed = json.loads(_text())
    changed["paths"]["/pets"]["get"]["operationId"] = "listAllPets"
    assert fingerprint.fingerprint(normalize(json.loads(_text()))) != fingerprint.fingerprint(
        normalize(changed)
    )


def test_advisory_output_is_excluded_from_the_fingerprint():
    """Two runs that describe the same API are the same IR, even if one also
    mentioned a missing licence field."""
    definition = normalize(json.loads(_text()))
    before = fingerprint.fingerprint(definition)
    definition.lint_findings = []
    definition.warnings = []
    assert fingerprint.fingerprint(definition) == before


def test_the_ir_carries_its_version():
    definition = normalize(json.loads(_text()))
    assert definition.ir_version == IR_VERSION
    assert fingerprint.describe(definition)["ir_version"] == IR_VERSION


def test_the_content_hash_is_of_the_raw_document():
    assert fingerprint.content_hash("a") != fingerprint.content_hash("b")
    assert len(fingerprint.content_hash("a")) == 64


# ── Stage reporting ──────────────────────────────────────────────────────────


def test_every_stage_is_reported_in_order():
    report = translate(_text())
    assert report.ok
    assert [stage.name for stage in report.stages] == [
        "parse",
        "convert",
        "validate",
        "lint",
        "resolve",
        "normalize",
    ]
    assert all(stage.status in (OK, SKIPPED) for stage in report.stages)


def test_conversion_is_skipped_with_a_reason_for_openapi_3():
    report = translate(_text())
    convert = next(stage for stage in report.stages if stage.name == "convert")
    assert convert.status == SKIPPED
    assert "already OpenAPI 3" in convert.detail["reason"]


def test_a_swagger_2_document_runs_the_conversion_stage():
    swagger2 = {
        "swagger": "2.0",
        "info": {"title": "Legacy", "version": "1.0"},
        "host": "api.example.com",
        "basePath": "/v2",
        "schemes": ["https"],
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "summary": "List.",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    report = translate(json.dumps(swagger2))
    convert = next(stage for stage in report.stages if stage.name == "convert")
    assert convert.status == OK
    assert convert.detail["from"] == "swagger2"
    assert report.definition.source_dialect == "swagger2"


def test_the_normalize_stage_reports_what_it_produced():
    report = translate(_text())
    normalize_stage = next(stage for stage in report.stages if stage.name == "normalize")
    assert normalize_stage.detail["operations"] == 1
    assert normalize_stage.detail["servers"] == 1
    assert normalize_stage.detail["security_schemes"] == 1
    assert normalize_stage.detail["ir_hash"] == report.ir_hash


def test_the_lint_stage_reports_counts_without_failing():
    report = translate(_text())
    lint = next(stage for stage in report.stages if stage.name == "lint")
    assert lint.status == OK
    assert set(lint.detail["counts"]) == {"error", "warning", "info"}


def test_stages_carry_a_duration():
    report = translate(_text())
    assert all(stage.duration_ms >= 0 for stage in report.stages)
    assert report.as_dict()["total_duration_ms"] >= 0


# ── Failure halts the chain, and says where ──────────────────────────────────


def test_a_parse_failure_halts_at_parse():
    report = translate("{not json at all")
    assert report.ok is False
    assert report.failed_stage.name == "parse"
    assert report.failed_stage.error_code == "parse_error"
    assert len(report.stages) == 1, "nothing should run after a failed stage"


def test_a_semantic_error_halts_before_the_ir():
    """LLD §3.4: semantic errors halt the pipeline before IR generation."""
    report = translate(json.dumps({"openapi": "3.0.0", "info": {}, "paths": {"/a": {"get": {}}}}))
    assert report.ok is False
    assert report.failed_stage.name == "validate"
    assert report.definition is None
    assert report.ir_hash is None
    assert "normalize" not in [stage.name for stage in report.stages]


def test_a_validation_failure_still_reports_every_violation():
    report = translate(json.dumps({"openapi": "3.0.0", "info": {}, "paths": {"/a": {"get": {}}}}))
    assert len(report.findings) >= 3


def test_a_failed_translation_never_raises():
    """The same function serves an interactive import and an unattended sync
    check; a failure is an outcome to report, not an exception to handle."""
    for content in ("", "{}", "not a document", json.dumps({"openapi": "9.9"})):
        report = translate(content)
        assert report.ok is False
        assert report.failed_stage is not None


def test_the_report_serializes():
    payload = translate(_text()).as_dict()
    assert payload["ok"] is True
    assert payload["failed_stage"] is None
    assert json.dumps(payload)


def test_migrating_dialect_without_changing_the_api_is_not_drift():
    """A team moving swagger.json → openapi.json describes the same API.
    Reporting that as drift is the reformat noise this design avoids."""
    from sutr.openapi.diff import compare

    swagger2 = {
        "swagger": "2.0",
        "info": {"title": "Things", "version": "1.0.0"},
        "host": "api.example.com",
        "basePath": "/v1",
        "schemes": ["https"],
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "summary": "List.",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    openapi3 = {
        "openapi": "3.0.3",
        "info": {"title": "Things", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "summary": "List.",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    from_swagger = translate(json.dumps(swagger2))
    from_openapi = translate(json.dumps(openapi3))

    assert from_swagger.definition.source_dialect == "swagger2"
    assert from_openapi.definition.source_dialect == "openapi3"
    # Same API, so the same fingerprint and no drift.
    assert from_swagger.ir_hash == from_openapi.ir_hash
    assert compare(from_swagger.definition, from_openapi.definition).changes == []
