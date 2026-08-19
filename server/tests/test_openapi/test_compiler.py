"""Compiler unit tests: naming, collisions, filtering, params, security, servers."""

import pytest

from agent_port.openapi.compiler import CompileFilters, assign_tool_names, compile_definition
from agent_port.openapi.errors import OpenAPIError
from agent_port.openapi.normalizer import normalize, substitute_server_url
from agent_port.openapi.security import translate_security


def _spec(paths: dict, *, components: dict | None = None, security=None, servers=None) -> dict:
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": paths,
    }
    if components:
        doc["components"] = components
    if security is not None:
        doc["security"] = security
    if servers is not None:
        doc["servers"] = servers
    return doc


def _op(summary="Do the thing", operation_id=None, tags=None, **extra) -> dict:
    op = {"summary": summary, "responses": {"200": {"description": "OK"}}}
    if operation_id:
        op["operationId"] = operation_id
    if tags:
        op["tags"] = tags
    op.update(extra)
    return op


def test_basic_compile_names_from_operation_id():
    definition = normalize(_spec({"/users/{id}": {"get": _op(operation_id="getUserById")}}))
    result = compile_definition(definition)
    tool = result.tools[0].tool
    assert tool.name == "get_user_by_id"
    assert tool.method == "GET"
    assert tool.path == "/users/{id}"


def test_name_from_method_and_path_when_no_operation_id():
    definition = normalize(_spec({"/admin/users": {"get": _op()}}))
    result = compile_definition(definition)
    assert result.tools[0].tool.name == "get_admin_users"


def test_collision_resolved_deterministically():
    # Both operations claim operationId list_users → both get path-qualified names.
    definition = normalize(
        _spec(
            {
                "/users": {"get": _op(operation_id="list_users")},
                "/admin/users": {"get": _op(operation_id="list_users")},
            }
        )
    )
    result = compile_definition(definition)
    names = sorted(ct.tool.name for ct in result.tools)
    assert names == ["get_admin_users", "get_users"]
    assert all(ct.renamed_from == "list_users" for ct in result.tools)


def test_collision_with_identical_paths_gets_numeric_suffix():
    ops = [
        type("Op", (), {})()  # placeholder — built via normalize below instead
    ]
    definition = normalize(
        _spec(
            {
                "/users": {
                    "get": _op(operation_id="users_op"),
                    "post": _op(operation_id="users_op"),
                }
            }
        )
    )
    names = assign_tool_names(definition.operations)
    finals = sorted(name for name, _ in names.values())
    assert finals == ["get_users", "post_users"]
    del ops


def test_path_params_and_query_params():
    definition = normalize(
        _spec(
            {
                "/items/{item_id}": {
                    "get": _op(
                        operation_id="get_item",
                        parameters=[
                            {
                                "name": "item_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "integer"},
                            },
                            {"name": "expand", "in": "query", "schema": {"type": "string"}},
                            {
                                "name": "X-Trace-Id",
                                "in": "header",
                                "schema": {"type": "string"},
                            },
                        ],
                    )
                }
            }
        )
    )
    tool = compile_definition(definition).tools[0].tool
    by_name = {p.name: p for p in tool.params}
    assert by_name["item_id"].location == "path"
    assert by_name["item_id"].type == "integer"
    assert by_name["item_id"].required
    assert by_name["expand"].location == "query"
    header = by_name["X_Trace_Id"]
    assert header.location == "header"
    assert header.wire_name == "X-Trace-Id"


def test_dashed_path_param_is_rewritten():
    definition = normalize(
        _spec(
            {
                "/users/{user-id}": {
                    "get": _op(
                        operation_id="get_user",
                        parameters=[
                            {
                                "name": "user-id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"},
                            }
                        ],
                    )
                }
            }
        )
    )
    tool = compile_definition(definition).tools[0].tool
    assert tool.path == "/users/{user_id}"
    assert tool.params[0].name == "user_id"


def test_object_body_flattened():
    definition = normalize(
        _spec(
            {
                "/refunds": {
                    "post": _op(
                        operation_id="create_refund",
                        requestBody={
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["customer_id"],
                                        "properties": {
                                            "customer_id": {"type": "string"},
                                            "amount": {"type": "number"},
                                            "metadata": {"type": "object"},
                                        },
                                    }
                                }
                            },
                        },
                    )
                }
            }
        )
    )
    tool = compile_definition(definition).tools[0].tool
    by_name = {p.name: p for p in tool.params}
    assert tool.body_param is None
    assert by_name["customer_id"].required
    assert not by_name["amount"].required
    assert by_name["metadata"].schema_override == {"type": "object"}


def test_array_body_wrapped():
    definition = normalize(
        _spec(
            {
                "/batch": {
                    "post": _op(
                        operation_id="batch_create",
                        requestBody={
                            "content": {
                                "application/json": {
                                    "schema": {"type": "array", "items": {"type": "string"}}
                                }
                            }
                        },
                    )
                }
            }
        )
    )
    tool = compile_definition(definition).tools[0].tool
    assert tool.body_param == "body"
    assert tool.params[0].schema_override["type"] == "array"


def test_non_json_body_warns():
    definition = normalize(
        _spec(
            {
                "/upload": {
                    "post": _op(
                        operation_id="upload_file",
                        requestBody={
                            "content": {"multipart/form-data": {"schema": {"type": "object"}}}
                        },
                    )
                }
            }
        )
    )
    assert any(w.code == "unsupported_body" for w in definition.warnings)


def test_filters_by_tag_and_path():
    definition = normalize(
        _spec(
            {
                "/users": {"get": _op(operation_id="lu", tags=["users"])},
                "/orders": {"get": _op(operation_id="lo", tags=["orders"])},
                "/internal/debug": {"get": _op(operation_id="dbg", tags=["internal"])},
            }
        )
    )
    result = compile_definition(definition, CompileFilters(include_tags=["users", "orders"]))
    assert {ct.tool.name for ct in result.tools} == {"lu", "lo"}

    result = compile_definition(definition, CompileFilters(exclude_paths=["/internal/*"]))
    assert {ct.tool.name for ct in result.tools} == {"lu", "lo"}

    with pytest.raises(OpenAPIError) as exc:
        compile_definition(definition, CompileFilters(include_tags=["nonexistent"]))
    assert exc.value.code == "nothing_selected"


def test_deprecated_excluded_by_default():
    definition = normalize(
        _spec(
            {
                "/old": {"get": _op(operation_id="old_op", deprecated=True)},
                "/new": {"get": _op(operation_id="new_op")},
            }
        )
    )
    result = compile_definition(definition)
    assert [ct.tool.name for ct in result.tools] == ["new_op"]
    result = compile_definition(definition, CompileFilters(include_deprecated=True))
    assert {ct.tool.name for ct in result.tools} == {"old_op", "new_op"}


def test_description_is_factual():
    definition = normalize(
        _spec(
            {
                "/pets": {
                    "get": _op(
                        summary="List pets",
                        operation_id="list_pets",
                        description="Returns all pets the caller can see.",
                    )
                }
            }
        )
    )
    desc = compile_definition(definition).tools[0].tool.description
    assert "List pets." in desc
    assert "Returns all pets the caller can see." in desc
    assert "HTTP GET /pets." in desc
    assert "Returns: OK." in desc


def test_security_translation_api_key_header():
    definition = normalize(
        _spec(
            {"/x": {"get": _op(operation_id="x")}},
            components={
                "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}
            },
            security=[{"key": []}],
        )
    )
    auth = translate_security(definition)
    assert auth.token_header == "X-Api-Key"
    assert auth.token_format == "{token}"


def test_security_translation_bearer_and_basic():
    for scheme, expected in [
        ({"type": "http", "scheme": "bearer"}, "Bearer {token}"),
        ({"type": "http", "scheme": "basic"}, "Basic {token}"),
        ({"type": "oauth2", "flows": {}}, "Bearer {token}"),
    ]:
        definition = normalize(
            _spec(
                {"/x": {"get": _op(operation_id="x")}},
                components={"securitySchemes": {"s": scheme}},
                security=[{"s": []}],
            )
        )
        auth = translate_security(definition)
        assert auth.token_format == expected, scheme


def test_security_translation_query_api_key_warns():
    definition = normalize(
        _spec(
            {"/x": {"get": _op(operation_id="x")}},
            components={
                "securitySchemes": {"qk": {"type": "apiKey", "in": "query", "name": "api_key"}}
            },
            security=[{"qk": []}],
        )
    )
    auth = translate_security(definition)
    assert auth.scheme_name is None
    assert any(w.code == "unsupported_security_scheme" for w in auth.warnings)


def test_server_variable_substitution():
    definition = normalize(
        _spec(
            {"/x": {"get": _op(operation_id="x")}},
            servers=[
                {
                    "url": "https://{environment}.example.com/{version}",
                    "variables": {
                        "environment": {"default": "api", "enum": ["api", "staging"]},
                        "version": {"default": "v1"},
                    },
                }
            ],
        )
    )
    server = definition.servers[0]
    assert substitute_server_url(server) == "https://api.example.com/v1"
    assert (
        substitute_server_url(server, {"environment": "staging"})
        == "https://staging.example.com/v1"
    )
    with pytest.raises(OpenAPIError) as exc:
        substitute_server_url(server, {"environment": "prod"})
    assert exc.value.code == "invalid_server_variable"
