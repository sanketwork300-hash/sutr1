"""Drift classification (ESDS LLD §3.3, build prompt §13).

The taxonomy is the point. A change classified BREAKING blocks an automatic
apply; one classified DOCUMENTATION does not. Getting that wrong in either
direction is expensive — a false BREAKING trains people to click through
warnings, and a false NON_BREAKING deploys something that breaks callers.
"""

import json

from sutr.openapi.diff import Category, compare
from sutr.openapi.normalizer import normalize

BASE = {
    "openapi": "3.0.3",
    "info": {"title": "Pets", "version": "1.0.0", "description": "The pet API."},
    "servers": [{"url": "https://api.example.com/v1"}],
    "components": {
        "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}
    },
    "security": [{"apiKey": []}],
    "tags": [{"name": "pets", "description": "Pets."}],
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets.",
                "tags": ["pets"],
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "status", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "A list."}},
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet.",
                "tags": ["pets"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "tag": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "Created."}},
            },
        }
    },
}


def _mutate(**changes):
    """A copy of BASE with a mutation applied, normalized."""
    document = json.loads(json.dumps(BASE))
    changes["apply"](document)
    return normalize(document)


def _diff(mutation):
    return compare(normalize(json.loads(json.dumps(BASE))), _mutate(apply=mutation))


def _codes(report, category=None):
    return {
        change.code for change in report.changes if category is None or change.category is category
    }


# ── No change ────────────────────────────────────────────────────────────────


def test_an_identical_document_produces_no_changes():
    report = compare(
        normalize(json.loads(json.dumps(BASE))), normalize(json.loads(json.dumps(BASE)))
    )
    assert report.changes == []
    assert report.has_changes is False


def test_reordering_keys_is_not_a_change():
    """Drift is compared over the IR, not the document. A reformat that reports
    as drift trains people to ignore drift."""
    reordered = json.loads(json.dumps(BASE))
    reordered["paths"]["/pets"]["get"]["parameters"].reverse()
    report = compare(normalize(json.loads(json.dumps(BASE))), normalize(reordered))
    assert report.changes == []


# ── BREAKING ─────────────────────────────────────────────────────────────────


def test_removing_an_operation_is_breaking():
    def mutate(doc):
        del doc["paths"]["/pets"]["post"]

    report = _diff(mutate)
    assert "operation_removed" in _codes(report, Category.BREAKING)


def test_making_a_parameter_required_is_breaking():
    def mutate(doc):
        doc["paths"]["/pets"]["get"]["parameters"][0]["required"] = True

    assert "parameter_became_required" in _codes(_diff(mutate), Category.BREAKING)


def test_adding_a_required_parameter_is_breaking_but_optional_is_not():
    def add_required(doc):
        doc["paths"]["/pets"]["get"]["parameters"].append(
            {"name": "tenant", "in": "query", "required": True, "schema": {"type": "string"}}
        )

    def add_optional(doc):
        doc["paths"]["/pets"]["get"]["parameters"].append(
            {"name": "cursor", "in": "query", "schema": {"type": "string"}}
        )

    assert "parameter_added" in _codes(_diff(add_required), Category.BREAKING)
    assert "parameter_added" in _codes(_diff(add_optional), Category.NON_BREAKING)


def test_removing_a_required_parameter_is_breaking():
    def mutate(doc):
        doc["paths"]["/pets"]["get"]["parameters"][0]["required"] = True

    # First make it required in "before", then remove it in "after".
    before = _mutate(apply=mutate)
    after_doc = json.loads(json.dumps(BASE))
    after_doc["paths"]["/pets"]["get"]["parameters"] = [
        p for p in after_doc["paths"]["/pets"]["get"]["parameters"] if p["name"] != "limit"
    ]
    report = compare(before, normalize(after_doc))
    assert "parameter_removed" in _codes(report, Category.BREAKING)


def test_changing_a_parameter_type_is_breaking():
    def mutate(doc):
        doc["paths"]["/pets"]["get"]["parameters"][0]["schema"]["type"] = "string"

    assert "parameter_type_changed" in _codes(_diff(mutate), Category.BREAKING)


def test_removing_a_server_is_breaking():
    def mutate(doc):
        doc["servers"] = [{"url": "https://api2.example.com/v1"}]

    report = _diff(mutate)
    assert "server_removed" in _codes(report, Category.BREAKING)
    assert "server_added" in _codes(report, Category.NON_BREAKING)


def test_changing_an_operation_id_is_breaking_because_it_renames_a_tool():
    def mutate(doc):
        doc["paths"]["/pets"]["get"]["operationId"] = "listAllPets"

    report = _diff(mutate)
    assert "operation_id_changed" in _codes(report, Category.BREAKING)
    assert any("renames the generated tool" in c.summary for c in report.changes)


def test_a_newly_required_body_property_is_breaking():
    def mutate(doc):
        doc["paths"]["/pets"]["post"]["requestBody"]["content"]["application/json"]["schema"][
            "required"
        ] = ["name", "tag"]

    assert "body_property_became_required" in _codes(_diff(mutate), Category.BREAKING)


def test_removing_a_required_body_property_is_breaking():
    def mutate(doc):
        schema = doc["paths"]["/pets"]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        del schema["properties"]["name"]

    assert "body_property_removed" in _codes(_diff(mutate), Category.BREAKING)


def test_removing_the_request_body_is_breaking():
    def mutate(doc):
        del doc["paths"]["/pets"]["post"]["requestBody"]

    assert "request_body_removed" in _codes(_diff(mutate), Category.BREAKING)


def test_changing_the_body_media_type_is_breaking():
    def mutate(doc):
        content = doc["paths"]["/pets"]["post"]["requestBody"]["content"]
        content["application/x-www-form-urlencoded"] = content.pop("application/json")

    assert "request_body_media_type_changed" in _codes(_diff(mutate), Category.BREAKING)


# ── NON_BREAKING ─────────────────────────────────────────────────────────────


def test_adding_an_operation_is_not_breaking():
    def mutate(doc):
        doc["paths"]["/pets/{id}"] = {
            "get": {
                "operationId": "getPet",
                "summary": "Get one.",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "One."}},
            }
        }

    assert "operation_added" in _codes(_diff(mutate), Category.NON_BREAKING)


def test_relaxing_a_requirement_is_not_breaking():
    before_doc = json.loads(json.dumps(BASE))
    before_doc["paths"]["/pets"]["get"]["parameters"][0]["required"] = True
    report = compare(normalize(before_doc), normalize(json.loads(json.dumps(BASE))))
    assert "parameter_became_optional" in _codes(report, Category.NON_BREAKING)


def test_deprecating_an_operation_is_not_breaking():
    def mutate(doc):
        doc["paths"]["/pets"]["get"]["deprecated"] = True

    assert "operation_deprecated" in _codes(_diff(mutate), Category.NON_BREAKING)


# ── SECURITY ─────────────────────────────────────────────────────────────────


def test_adding_authentication_is_a_security_change_not_a_safe_one():
    """Adding auth breaks existing callers; it is not 'non-breaking because
    it is safer'."""

    def mutate(doc):
        doc["components"]["securitySchemes"]["bearer"] = {"type": "http", "scheme": "bearer"}
        doc["security"] = [{"apiKey": []}, {"bearer": []}]

    report = _diff(mutate)
    assert "security_scheme_added" in _codes(report, Category.SECURITY)


def test_removing_authentication_is_a_security_change():
    def mutate(doc):
        doc["components"]["securitySchemes"] = {}
        doc["security"] = []

    report = _diff(mutate)
    assert "security_scheme_removed" in _codes(report, Category.SECURITY)
    assert "global_security_changed" in _codes(report, Category.SECURITY)


def test_moving_a_credential_is_a_security_change():
    def mutate(doc):
        doc["components"]["securitySchemes"]["apiKey"]["in"] = "query"

    assert "security_scheme_changed" in _codes(_diff(mutate), Category.SECURITY)


def test_changing_an_operations_security_is_a_security_change():
    def mutate(doc):
        doc["paths"]["/pets"]["get"]["security"] = []

    assert "operation_security_changed" in _codes(_diff(mutate), Category.SECURITY)


# ── DOCUMENTATION and METADATA ───────────────────────────────────────────────


def test_changing_a_summary_is_documentation():
    def mutate(doc):
        doc["paths"]["/pets"]["get"]["summary"] = "List every pet."

    report = _diff(mutate)
    assert "operation_documentation_changed" in _codes(report, Category.DOCUMENTATION)
    assert report.breaking == []


def test_changing_the_api_version_is_metadata():
    def mutate(doc):
        doc["info"]["version"] = "2.0.0"

    assert "api_version_changed" in _codes(_diff(mutate), Category.METADATA)


def test_changing_tags_is_metadata():
    def mutate(doc):
        doc["paths"]["/pets"]["get"]["tags"] = ["animals"]

    assert "operation_tags_changed" in _codes(_diff(mutate), Category.METADATA)


def test_an_undocumented_response_is_documentation_not_breaking():
    """The server may still return it; the document stopping mentioning it does
    not break a caller."""

    def mutate(doc):
        doc["paths"]["/pets"]["get"]["responses"]["500"] = {"description": "Boom."}

    assert "response_added" in _codes(_diff(mutate), Category.DOCUMENTATION)


# ── The report ───────────────────────────────────────────────────────────────


def test_changes_are_ordered_with_breaking_first():
    def mutate(doc):
        doc["info"]["version"] = "2.0.0"
        doc["paths"]["/pets"]["get"]["summary"] = "Changed."
        del doc["paths"]["/pets"]["post"]

    report = _diff(mutate)
    assert report.changes[0].category is Category.BREAKING


def test_counts_cover_every_category():
    report = _diff(lambda doc: doc["info"].update({"version": "2.0.0"}))
    assert set(report.counts()) == {c.value for c in Category}


def test_the_report_serializes_for_storage():
    report = _diff(lambda doc: doc["info"].update({"version": "2.0.0"}))
    payload = report.as_dict()
    assert payload["has_changes"] is True
    assert payload["changes"][0]["category"] == "METADATA"
    assert json.dumps(payload)
