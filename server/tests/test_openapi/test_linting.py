"""Spectral-style OpenAPI linting (build prompt §16).

The requirement has four parts and each is pinned here: every finding carries
a rule id / severity / message / location / JSON pointer / documentation /
remediation; findings are returned in full rather than one at a time; the
severity ladder is ERROR / WARNING / INFO; and linting is advisory — a clean
specification produces no errors and a messy one still imports.
"""

import json

import pytest

from sutr.openapi.errors import OpenAPIError
from sutr.openapi.linting import Severity, all_rules, lint_document
from sutr.openapi.linting.finding import pointer
from sutr.openapi.linting.schema_errors import schema_findings
from sutr.openapi.normalizer import normalize, validate_spec

CLEAN = {
    "openapi": "3.0.3",
    "info": {
        "title": "Payments",
        "version": "1.0.0",
        "description": "Move money between accounts.",
        "contact": {"name": "Platform", "email": "api@example.com"},
        "license": {"name": "MIT"},
    },
    "servers": [{"url": "https://api.example.com/v1"}],
    "tags": [{"name": "payments", "description": "Payment operations."}],
    "security": [{"apiKey": []}],
    "components": {
        "securitySchemes": {"apiKey": {"type": "apiKey", "name": "X-Api-Key", "in": "header"}}
    },
    "paths": {
        "/payments/{paymentId}": {
            "get": {
                "operationId": "getPayment",
                "summary": "Fetch one payment.",
                "description": "Returns a single payment by id.",
                "tags": ["payments"],
                "parameters": [
                    {
                        "name": "paymentId",
                        "in": "path",
                        "required": True,
                        "description": "The payment id.",
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "The payment."}},
            }
        }
    },
}


def _ids(findings) -> set:
    return {f.rule_id for f in findings}


def test_a_well_formed_specification_produces_no_errors_or_warnings():
    report = lint_document(CLEAN)
    assert report.errors == [], [f.message for f in report.errors]
    assert report.warnings == [], [f.message for f in report.warnings]


def test_every_finding_carries_the_full_required_shape():
    report = lint_document({"openapi": "3.0.0", "info": {}, "paths": {}})
    assert report.findings
    for finding in report.findings:
        assert finding.rule_id
        assert finding.severity in (Severity.ERROR, Severity.WARNING, Severity.INFO)
        assert finding.message
        assert finding.location
        assert finding.json_pointer.startswith("/") or finding.json_pointer == ""
        assert finding.documentation.startswith("/openapi-linting#")
        assert finding.documentation.endswith(finding.rule_id)
        assert finding.remediation


def test_findings_are_ordered_errors_first():
    report = lint_document({"openapi": "3.0.0", "info": {}, "paths": {}})
    severities = [f.severity for f in report.findings]
    assert severities == sorted(severities, key=lambda s: ["ERROR", "WARNING", "INFO"].index(s))


def test_rule_ids_are_unique_and_documented():
    rules = all_rules()
    assert len(rules) == len({r.id for r in rules})
    assert all(r.summary for r in rules)


# ── Individual rules ─────────────────────────────────────────────────────────


def test_missing_servers_is_an_error():
    doc = {**CLEAN}
    doc.pop("servers")
    assert "oas3-api-servers" in _ids(lint_document(doc).errors)


def test_duplicate_operation_ids_are_an_error():
    doc = json.loads(json.dumps(CLEAN))
    doc["paths"]["/other"] = {
        "get": {
            "operationId": "getPayment",
            "summary": "Duplicate.",
            "tags": ["payments"],
            "responses": {"200": {"description": "ok"}},
        }
    }
    assert "operation-operationId-unique" in _ids(lint_document(doc).errors)


def test_path_template_and_declared_parameters_must_agree():
    doc = json.loads(json.dumps(CLEAN))
    doc["paths"]["/payments/{paymentId}"]["get"]["parameters"] = []
    findings = [f for f in lint_document(doc).errors if f.rule_id == "path-params"]
    assert findings
    assert "paymentId" in findings[0].message


def test_a_declared_path_parameter_absent_from_the_path_is_reported():
    doc = json.loads(json.dumps(CLEAN))
    doc["paths"]["/payments/{paymentId}"]["get"]["parameters"].append(
        {"name": "ghost", "in": "path", "required": True, "schema": {"type": "string"}}
    )
    messages = [f.message for f in lint_document(doc).errors if f.rule_id == "path-params"]
    assert any("ghost" in m for m in messages)


def test_duplicate_parameters_are_an_error():
    doc = json.loads(json.dumps(CLEAN))
    doc["paths"]["/payments/{paymentId}"]["get"]["parameters"].append(
        {"name": "paymentId", "in": "path", "required": True, "schema": {"type": "string"}}
    )
    assert "operation-parameters" in _ids(lint_document(doc).errors)


def test_undefined_security_requirement_is_an_error():
    doc = json.loads(json.dumps(CLEAN))
    doc["security"] = [{"nonexistent": []}]
    assert "oas3-operation-security-defined" in _ids(lint_document(doc).errors)


def test_incomplete_api_key_scheme_is_an_error():
    doc = json.loads(json.dumps(CLEAN))
    doc["components"]["securitySchemes"]["apiKey"] = {"type": "apiKey"}
    assert "sutr-api-key-location" in _ids(lint_document(doc).errors)


def test_oauth2_without_flows_is_an_error():
    doc = json.loads(json.dumps(CLEAN))
    doc["components"]["securitySchemes"]["oauth"] = {"type": "oauth2"}
    doc["security"] = [{"apiKey": []}, {"oauth": []}]
    assert "sutr-oauth2-flows" in _ids(lint_document(doc).errors)


def test_oauth2_client_credentials_without_token_url_is_an_error():
    doc = json.loads(json.dumps(CLEAN))
    doc["components"]["securitySchemes"]["oauth"] = {
        "type": "oauth2",
        "flows": {"clientCredentials": {"scopes": {}}},
    }
    doc["security"] = [{"apiKey": []}, {"oauth": []}]
    messages = [f.message for f in lint_document(doc).errors if f.rule_id == "sutr-oauth2-flows"]
    assert any("tokenUrl" in m for m in messages)


def test_ref_siblings_are_an_error_on_3_0_only():
    doc = json.loads(json.dumps(CLEAN))
    doc["paths"]["/payments/{paymentId}"]["get"]["responses"]["200"] = {
        "$ref": "#/components/responses/Ok",
        "description": "shadowed",
    }
    assert "no-$ref-siblings" in _ids(lint_document(doc).errors)

    doc31 = json.loads(json.dumps(doc))
    doc31["openapi"] = "3.1.0"
    assert "no-$ref-siblings" not in _ids(lint_document(doc31).findings)


def test_missing_operation_description_is_a_warning():
    doc = json.loads(json.dumps(CLEAN))
    del doc["paths"]["/payments/{paymentId}"]["get"]["summary"]
    del doc["paths"]["/payments/{paymentId}"]["get"]["description"]
    assert "operation-description" in _ids(lint_document(doc).warnings)


def test_undeclared_tag_is_a_warning():
    doc = json.loads(json.dumps(CLEAN))
    doc["paths"]["/payments/{paymentId}"]["get"]["tags"] = ["invented"]
    assert "operation-tag-defined" in _ids(lint_document(doc).warnings)


def test_script_tag_in_a_description_is_a_warning():
    doc = json.loads(json.dumps(CLEAN))
    doc["info"]["description"] = "Docs <script>alert(1)</script>"
    assert "no-script-tags-in-markdown" in _ids(lint_document(doc).warnings)


def test_enum_type_mismatch_and_duplicates_are_warnings():
    doc = json.loads(json.dumps(CLEAN))
    doc["paths"]["/payments/{paymentId}"]["get"]["parameters"][0]["schema"] = {
        "type": "string",
        "enum": ["a", "a", 3],
    }
    ids = _ids(lint_document(doc).warnings)
    assert "duplicated-entry-in-enum" in ids
    assert "typed-enum" in ids


def test_unused_component_is_info_only():
    doc = json.loads(json.dumps(CLEAN))
    doc["components"]["schemas"] = {"Orphan": {"type": "object"}}
    assert "oas3-unused-component" in _ids(lint_document(doc).infos)


def test_a_rule_that_raises_does_not_stop_the_others(monkeypatch):
    from sutr.openapi.linting import registry

    def explode(_document):
        raise RuntimeError("boom")

    broken = registry.Rule(
        id="zz-broken", severity=Severity.ERROR, summary="explodes", check=explode
    )
    monkeypatch.setattr(registry, "_RULES", {**registry._RULES, "zz-broken": broken})
    report = lint_document({"openapi": "3.0.0", "info": {}, "paths": {}})
    assert report.findings, "a broken rule must not suppress every other finding"


# ── Structural validation returns every error ────────────────────────────────


def test_schema_validation_reports_every_error_not_just_the_first():
    broken = {"openapi": "3.0.0", "info": {}, "paths": {"/a": {"get": {}}}}
    findings = schema_findings(broken, "3.0.0")
    assert len(findings) >= 3
    assert all(f.rule_id == "oas3-schema" for f in findings)
    assert {f.json_pointer for f in findings} != {""}


def test_validate_spec_raises_with_all_findings_attached():
    broken = {"openapi": "3.0.0", "info": {}, "paths": {"/a": {"get": {}}}}
    with pytest.raises(OpenAPIError) as excinfo:
        validate_spec(broken)
    assert excinfo.value.code == "invalid_spec"
    assert len(excinfo.value.findings) >= 3
    assert "and" in excinfo.value.message and "more" in excinfo.value.message


def test_lint_findings_travel_on_the_normalized_definition():
    doc = json.loads(json.dumps(CLEAN))
    del doc["paths"]["/payments/{paymentId}"]["get"]["operationId"]
    definition = normalize(doc)
    assert "operation-operationId" in {f.rule_id for f in definition.lint_findings}


def test_linting_never_blocks_an_otherwise_valid_import():
    doc = json.loads(json.dumps(CLEAN))
    doc["info"]["description"] = ""
    del doc["tags"]
    definition = normalize(doc)  # must not raise
    assert definition.operations
    assert definition.lint_findings


def test_json_pointer_escaping():
    assert pointer("paths", "/a/b", "get") == "/paths/~1a~1b/get"
    assert pointer("a~b") == "/a~0b"
    assert pointer() == ""


def test_the_generated_rule_reference_covers_every_rule():
    """Every finding's `documentation` field points at an anchor on
    docs/pages/openapi-linting.md. A rule added without regenerating that page
    would hand users a dangling link, so the check lives here rather than in
    someone's memory. Regenerate with:

        uv run python scripts/generate_lint_docs.py
    """
    import pathlib

    page = pathlib.Path(__file__).resolve().parents[3] / "docs" / "pages" / "openapi-linting.md"
    assert page.exists(), "the generated rule reference is missing"
    text = page.read_text(encoding="utf-8")
    missing = [entry.id for entry in all_rules() if f"#### `{entry.id}`" not in text]
    assert missing == [], (
        f"rules missing from the reference: {missing}. "
        "Run: uv run python scripts/generate_lint_docs.py"
    )
