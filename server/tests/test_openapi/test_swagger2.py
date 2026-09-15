"""Swagger 2.0 → OpenAPI 3.0 conversion (build prompt §17).

The requirement is that paths, parameters, definitions, security, consumes,
produces and responses all survive the conversion, and that anything which
cannot be represented produces an actionable message rather than vanishing.
Each of those is a test below.
"""

import json

import pytest

from sutr.openapi.errors import OpenAPIError
from sutr.openapi.normalizer import normalize
from sutr.openapi.swagger2 import convert, is_swagger2

PETSTORE_2 = {
    "swagger": "2.0",
    "info": {"title": "Petstore", "version": "1.0.0", "description": "Pets."},
    "host": "petstore.example.com",
    "basePath": "/v2",
    "schemes": ["https"],
    "consumes": ["application/json"],
    "produces": ["application/json"],
    "securityDefinitions": {
        "apiKey": {"type": "apiKey", "name": "api_key", "in": "header"},
        "basicAuth": {"type": "basic"},
        "petstoreAuth": {
            "type": "oauth2",
            "flow": "accessCode",
            "authorizationUrl": "https://petstore.example.com/oauth/authorize",
            "tokenUrl": "https://petstore.example.com/oauth/token",
            "scopes": {"read:pets": "read pets"},
        },
    },
    "security": [{"apiKey": []}],
    "definitions": {
        "Pet": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "id": {"type": "integer", "format": "int64"},
                "name": {"type": "string"},
                "tags": {"type": "array", "items": {"$ref": "#/definitions/Tag"}},
            },
        },
        "Tag": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
    "paths": {
        "/pets": {
            "get": {
                "operationId": "listPets",
                "summary": "List pets.",
                "parameters": [
                    {
                        "name": "status",
                        "in": "query",
                        "type": "array",
                        "items": {"type": "string"},
                        "collectionFormat": "multi",
                        "description": "Filter by status.",
                    }
                ],
                "responses": {
                    "200": {
                        "description": "A list of pets.",
                        "schema": {"type": "array", "items": {"$ref": "#/definitions/Pet"}},
                        "headers": {"X-Total": {"type": "integer", "description": "Total."}},
                    }
                },
            },
            "post": {
                "operationId": "createPet",
                "summary": "Create a pet.",
                "parameters": [
                    {
                        "name": "body",
                        "in": "body",
                        "required": True,
                        "description": "The pet to create.",
                        "schema": {"$ref": "#/definitions/Pet"},
                    }
                ],
                "responses": {"201": {"description": "Created."}},
            },
        },
        "/pets/{petId}/photo": {
            "post": {
                "operationId": "uploadPhoto",
                "summary": "Upload a photo.",
                "consumes": ["multipart/form-data"],
                "parameters": [
                    {"name": "petId", "in": "path", "required": True, "type": "string"},
                    {"name": "caption", "in": "formData", "type": "string", "required": True},
                    {"name": "file", "in": "formData", "type": "file"},
                ],
                "responses": {"200": {"description": "Uploaded."}},
            }
        },
    },
}


def _convert(document=None):
    return convert(document or json.loads(json.dumps(PETSTORE_2)))


def test_detects_swagger2():
    assert is_swagger2({"swagger": "2.0"})
    assert not is_swagger2({"openapi": "3.0.0"})
    assert not is_swagger2({})


def test_refuses_to_convert_a_non_swagger2_document():
    with pytest.raises(OpenAPIError) as excinfo:
        convert({"openapi": "3.0.0"})
    assert excinfo.value.code == "not_swagger2"


def test_refuses_swagger_versions_other_than_2_0():
    with pytest.raises(OpenAPIError) as excinfo:
        convert({"swagger": "2.5"})
    assert excinfo.value.code == "unsupported_version"


def test_host_base_path_and_schemes_become_servers():
    converted, _ = _convert()
    assert converted["openapi"].startswith("3.0")
    assert converted["servers"] == [{"url": "https://petstore.example.com/v2"}]


def test_multiple_schemes_produce_one_server_each():
    source = json.loads(json.dumps(PETSTORE_2))
    source["schemes"] = ["https", "http"]
    converted, _ = convert(source)
    assert [s["url"] for s in converted["servers"]] == [
        "https://petstore.example.com/v2",
        "http://petstore.example.com/v2",
    ]


def test_a_missing_scheme_is_assumed_https_and_said_so():
    source = json.loads(json.dumps(PETSTORE_2))
    del source["schemes"]
    converted, warnings = convert(source)
    assert converted["servers"] == [{"url": "https://petstore.example.com/v2"}]
    assert "swagger2_scheme_assumed" in {w.code for w in warnings}


def test_a_missing_host_warns_rather_than_inventing_one():
    source = json.loads(json.dumps(PETSTORE_2))
    del source["host"]
    del source["basePath"]
    converted, warnings = convert(source)
    assert "servers" not in converted
    assert "swagger2_no_host" in {w.code for w in warnings}


def test_definitions_move_to_components_and_refs_are_rewritten():
    converted, _ = _convert()
    assert set(converted["components"]["schemas"]) == {"Pet", "Tag"}
    tags = converted["components"]["schemas"]["Pet"]["properties"]["tags"]
    assert tags["items"]["$ref"] == "#/components/schemas/Tag"
    body = converted["paths"]["/pets"]["post"]["requestBody"]
    assert body["content"]["application/json"]["schema"]["$ref"] == "#/components/schemas/Pet"


def test_body_parameter_becomes_a_request_body_using_consumes():
    converted, _ = _convert()
    post = converted["paths"]["/pets"]["post"]
    assert "parameters" not in post or all(p["in"] != "body" for p in post["parameters"])
    body = post["requestBody"]
    assert body["required"] is True
    assert body["description"] == "The pet to create."
    assert list(body["content"]) == ["application/json"]


def test_form_data_becomes_a_multipart_request_body_with_a_binary_file():
    converted, _ = _convert()
    upload = converted["paths"]["/pets/{petId}/photo"]["post"]
    body = upload["requestBody"]
    assert list(body["content"]) == ["multipart/form-data"]
    schema = body["content"]["multipart/form-data"]["schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["file"] == {"type": "string", "format": "binary"}
    assert schema["required"] == ["caption"]
    # The path parameter stays a parameter.
    assert [p["name"] for p in upload["parameters"]] == ["petId"]


def test_form_data_without_a_file_defaults_to_url_encoded():
    source = json.loads(json.dumps(PETSTORE_2))
    upload = source["paths"]["/pets/{petId}/photo"]["post"]
    del upload["consumes"]
    upload["parameters"] = [
        {"name": "petId", "in": "path", "required": True, "type": "string"},
        {"name": "caption", "in": "formData", "type": "string"},
    ]
    converted, _ = convert(source)
    body = converted["paths"]["/pets/{petId}/photo"]["post"]["requestBody"]
    assert list(body["content"]) == ["application/x-www-form-urlencoded"]


def test_parameter_type_keys_move_into_a_schema():
    converted, _ = _convert()
    param = converted["paths"]["/pets"]["get"]["parameters"][0]
    assert param["schema"] == {"type": "array", "items": {"type": "string"}}
    assert param["description"] == "Filter by status."
    assert "type" not in param


def test_collection_format_multi_becomes_form_explode():
    converted, _ = _convert()
    param = converted["paths"]["/pets"]["get"]["parameters"][0]
    assert param["style"] == "form"
    assert param["explode"] is True


def test_an_unmappable_collection_format_warns():
    source = json.loads(json.dumps(PETSTORE_2))
    source["paths"]["/pets"]["get"]["parameters"][0]["collectionFormat"] = "tsv"
    _, warnings = convert(source)
    assert "swagger2_collection_format" in {w.code for w in warnings}


def test_response_schema_and_produces_become_content():
    converted, _ = _convert()
    response = converted["paths"]["/pets"]["get"]["responses"]["200"]
    assert response["description"] == "A list of pets."
    assert list(response["content"]) == ["application/json"]
    assert response["content"]["application/json"]["schema"]["type"] == "array"
    assert response["headers"]["X-Total"]["schema"] == {"type": "integer"}


def test_security_definitions_are_translated():
    converted, _ = _convert()
    schemes = converted["components"]["securitySchemes"]
    assert schemes["apiKey"] == {"type": "apiKey", "name": "api_key", "in": "header"}
    assert schemes["basicAuth"] == {"type": "http", "scheme": "basic"}
    oauth = schemes["petstoreAuth"]
    assert set(oauth["flows"]) == {"authorizationCode"}
    assert oauth["flows"]["authorizationCode"]["tokenUrl"].endswith("/oauth/token")
    assert oauth["flows"]["authorizationCode"]["authorizationUrl"].endswith("/oauth/authorize")
    assert converted["security"] == [{"apiKey": []}]


def test_oauth_flow_names_are_renamed():
    source = json.loads(json.dumps(PETSTORE_2))
    source["securityDefinitions"]["petstoreAuth"] = {
        "type": "oauth2",
        "flow": "application",
        "tokenUrl": "https://petstore.example.com/oauth/token",
        "scopes": {},
    }
    converted, _ = convert(source)
    assert set(converted["components"]["securitySchemes"]["petstoreAuth"]["flows"]) == {
        "clientCredentials"
    }


def test_an_unknown_security_type_warns_and_is_skipped():
    source = json.loads(json.dumps(PETSTORE_2))
    source["securityDefinitions"]["weird"] = {"type": "magic"}
    converted, warnings = convert(source)
    assert "weird" not in converted["components"]["securitySchemes"]
    assert "swagger2_unknown_security_type" in {w.code for w in warnings}


def test_file_type_becomes_binary_string():
    source = json.loads(json.dumps(PETSTORE_2))
    source["paths"]["/pets"]["get"]["responses"]["200"]["schema"] = {"type": "file"}
    converted, _ = convert(source)
    content = converted["paths"]["/pets"]["get"]["responses"]["200"]["content"]
    assert content["application/octet-stream"]["schema"] == {"type": "string", "format": "binary"}


def test_string_discriminator_becomes_an_object():
    source = json.loads(json.dumps(PETSTORE_2))
    source["definitions"]["Pet"]["discriminator"] = "petType"
    converted, _ = convert(source)
    assert converted["components"]["schemas"]["Pet"]["discriminator"] == {"propertyName": "petType"}


def test_x_nullable_becomes_nullable():
    source = json.loads(json.dumps(PETSTORE_2))
    source["definitions"]["Pet"]["properties"]["name"]["x-nullable"] = True
    converted, _ = convert(source)
    assert converted["components"]["schemas"]["Pet"]["properties"]["name"]["nullable"] is True


def test_vendor_extensions_survive():
    source = json.loads(json.dumps(PETSTORE_2))
    source["x-audience"] = "internal"
    source["paths"]["/pets"]["get"]["x-rate-limit"] = 100
    converted, _ = convert(source)
    assert converted["x-audience"] == "internal"
    assert converted["paths"]["/pets"]["get"]["x-rate-limit"] == 100


def test_operation_level_schemes_are_reported_not_silently_dropped():
    source = json.loads(json.dumps(PETSTORE_2))
    source["paths"]["/pets"]["get"]["schemes"] = ["http"]
    _, warnings = convert(source)
    assert "swagger2_operation_schemes" in {w.code for w in warnings}


def test_two_body_parameters_are_reported():
    source = json.loads(json.dumps(PETSTORE_2))
    source["paths"]["/pets"]["post"]["parameters"].append(
        {"name": "second", "in": "body", "schema": {"type": "string"}}
    )
    _, warnings = convert(source)
    assert "swagger2_multiple_body_parameters" in {w.code for w in warnings}


# ── End to end through normalize() ───────────────────────────────────────────


def test_normalize_converts_swagger2_and_records_the_dialect():
    definition = normalize(json.loads(json.dumps(PETSTORE_2)))
    assert definition.source_dialect == "swagger2"
    assert definition.title == "Petstore"
    assert definition.openapi_version.startswith("3.0")
    assert [s.url for s in definition.servers] == ["https://petstore.example.com/v2"]
    ids = {op.operation_id for op in definition.operations}
    assert ids == {"listPets", "createPet", "uploadPhoto"}


def test_normalize_records_openapi3_dialect_for_a_native_document():
    definition = normalize(
        {
            "openapi": "3.0.3",
            "info": {"title": "t", "version": "1"},
            "servers": [{"url": "https://x.example.com"}],
            "paths": {
                "/a": {"get": {"operationId": "a", "responses": {"200": {"description": "k"}}}}
            },
        }
    )
    assert definition.source_dialect == "openapi3"


def test_converted_body_reaches_the_ir():
    definition = normalize(json.loads(json.dumps(PETSTORE_2)))
    create = next(op for op in definition.operations if op.operation_id == "createPet")
    assert create.request_body is not None
    assert create.request_body.content_type == "application/json"
    assert create.request_body.required is True


def test_conversion_warnings_reach_the_ir():
    source = json.loads(json.dumps(PETSTORE_2))
    del source["schemes"]
    definition = normalize(source)
    assert "swagger2_scheme_assumed" in {w.code for w in definition.warnings}
