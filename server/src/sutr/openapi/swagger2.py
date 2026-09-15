"""Swagger 2.0 → OpenAPI 3.0 conversion (build prompt §17).

Everything downstream of this module — validation, linting, `$ref` resolution,
the IR, the compiler, the packager — sees OpenAPI 3.x only. That is the whole
point: a second dialect threaded through the pipeline would double every code
path, so the dialect is normalised away at the front door instead.

The mapping is not invented. It is the standard, documented Swagger 2 → OAS 3
transformation, cross-checked against how `OpenAPITools/openapi-generator` and
`APIDevTools/swagger-parser` perform it (see docs/REFERENCE_MAP.md):

    host + basePath + schemes   → servers
    definitions                 → components.schemas
    parameters (global)         → components.parameters
    responses (global)          → components.responses
    securityDefinitions         → components.securitySchemes
    body parameter + consumes   → requestBody
    formData parameters         → requestBody (form-urlencoded or multipart)
    response.schema + produces  → response.content
    type: file                  → type: string, format: binary

Anything the mapping cannot represent produces a `SpecWarning` naming the
construct and where it was, rather than disappearing.
"""

import copy
import re
from typing import Any

from sutr.openapi.errors import OpenAPIError, SpecWarning

TARGET_VERSION = "3.0.3"

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")

# Keys that belong on a JSON Schema rather than on the OAS3 parameter object.
_SCHEMA_KEYS = (
    "type",
    "format",
    "items",
    "enum",
    "default",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "multipleOf",
)

_REF_MAP = (
    ("#/definitions/", "#/components/schemas/"),
    ("#/parameters/", "#/components/parameters/"),
    ("#/responses/", "#/components/responses/"),
)

# Swagger 2 collectionFormat → OAS3 (style, explode).
_COLLECTION_FORMATS = {
    "csv": ("form", False),
    "ssv": ("spaceDelimited", False),
    "pipes": ("pipeDelimited", False),
    "multi": ("form", True),
    "tsv": (None, None),  # no OAS3 equivalent
}

_OAUTH_FLOW_NAMES = {
    "implicit": "implicit",
    "password": "password",
    "application": "clientCredentials",
    "accessCode": "authorizationCode",
}


def is_swagger2(document: dict) -> bool:
    version = document.get("swagger")
    return isinstance(version, str) and version.startswith("2.")


def convert(document: dict) -> tuple[dict, list[SpecWarning]]:
    """Convert a Swagger 2.0 document to OpenAPI 3.0.

    Returns the converted document and any conversion warnings. Raises
    `OpenAPIError` only when the input is not a Swagger 2.0 document at all —
    structural problems are left to the normal validation path, which reports
    all of them at once.
    """
    if not is_swagger2(document):
        raise OpenAPIError(
            "not_swagger2",
            "The document does not declare `swagger: '2.0'`, so there is nothing to convert.",
        )
    version = str(document.get("swagger"))
    if not version.startswith("2.0"):
        raise OpenAPIError(
            "unsupported_version",
            f"Swagger {version} is not supported; only 2.0 can be converted to OpenAPI 3.x.",
        )

    source = copy.deepcopy(document)
    warnings: list[SpecWarning] = []

    converted: dict[str, Any] = {"openapi": TARGET_VERSION}
    if isinstance(source.get("info"), dict):
        converted["info"] = source["info"]
    if isinstance(source.get("externalDocs"), dict):
        converted["externalDocs"] = source["externalDocs"]
    if isinstance(source.get("tags"), list):
        converted["tags"] = source["tags"]

    servers = _servers(source, warnings)
    if servers:
        converted["servers"] = servers

    components: dict[str, Any] = {}
    if isinstance(source.get("definitions"), dict):
        components["schemas"] = {
            name: _convert_schema(schema, warnings, f"definitions.{name}")
            for name, schema in source["definitions"].items()
        }
    global_consumes = _string_list(source.get("consumes")) or ["application/json"]
    global_produces = _string_list(source.get("produces")) or ["application/json"]

    if isinstance(source.get("parameters"), dict):
        shared_params: dict[str, Any] = {}
        for name, param in source["parameters"].items():
            if not isinstance(param, dict):
                continue
            if param.get("in") in ("body", "formData"):
                warnings.append(
                    SpecWarning(
                        code="swagger2_shared_body_parameter",
                        message=(
                            f"Shared parameter '{name}' is a {param.get('in')} parameter. "
                            "OpenAPI 3 has no shared request-body parameter, so it was "
                            "inlined at each operation that referenced it."
                        ),
                        context=f"parameters.{name}",
                    )
                )
                continue
            shared_params[name] = _convert_parameter(param, warnings, f"parameters.{name}")
        if shared_params:
            components["parameters"] = shared_params

    if isinstance(source.get("responses"), dict):
        components["responses"] = {
            name: _convert_response(response, global_produces, warnings, f"responses.{name}")
            for name, response in source["responses"].items()
            if isinstance(response, dict)
        }

    schemes = _security_schemes(source, warnings)
    if schemes:
        components["securitySchemes"] = schemes

    if components:
        converted["components"] = components

    if isinstance(source.get("security"), list):
        converted["security"] = source["security"]

    converted["paths"] = _paths(
        source, global_consumes, global_produces, components.get("parameters"), warnings
    )

    for key, value in source.items():
        if key.startswith("x-"):
            converted[key] = value

    return _rewrite_refs(converted), warnings


# ── servers ──────────────────────────────────────────────────────────────────


def _servers(source: dict, warnings: list[SpecWarning]) -> list[dict]:
    host = source.get("host")
    base_path = source.get("basePath") or ""
    schemes = _string_list(source.get("schemes"))

    if not host:
        if base_path:
            return [{"url": base_path}]
        warnings.append(
            SpecWarning(
                code="swagger2_no_host",
                message=(
                    "The document declares neither `host` nor `basePath`, so no server URL "
                    "could be derived. Set the base URL manually before compiling tools."
                ),
                context="host",
            )
        )
        return []

    if not schemes:
        # Swagger 2 says "the scheme of the request the spec was served from";
        # there is no such request here, so https is the safe assumption and
        # the choice is surfaced rather than hidden.
        schemes = ["https"]
        warnings.append(
            SpecWarning(
                code="swagger2_scheme_assumed",
                message=(
                    "No `schemes` were declared; https was assumed for the server URL. "
                    "Change the server URL if the API is served over http."
                ),
                context="schemes",
            )
        )
    return [{"url": f"{scheme}://{host}{base_path}"} for scheme in schemes]


# ── paths & operations ───────────────────────────────────────────────────────


def _paths(
    source: dict,
    global_consumes: list[str],
    global_produces: list[str],
    shared_parameters: dict | None,
    warnings: list[SpecWarning],
) -> dict:
    paths: dict[str, Any] = {}
    for path, path_item in (source.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        converted_item: dict[str, Any] = {}
        for key, value in path_item.items():
            if key in ("$ref", "summary", "description") or key.startswith("x-"):
                converted_item[key] = value
        if isinstance(path_item.get("parameters"), list):
            shared, body_params = _split_parameters(
                path_item["parameters"], shared_parameters, warnings, str(path)
            )
            if shared:
                converted_item["parameters"] = shared
            if body_params:
                warnings.append(
                    SpecWarning(
                        code="swagger2_path_level_body",
                        message=(
                            "Body/formData parameters declared at the path level were applied "
                            "to every operation on that path, since OpenAPI 3 has no path-level "
                            "request body."
                        ),
                        context=str(path),
                    )
                )
        else:
            body_params = []

        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if isinstance(operation, dict):
                converted_item[method] = _convert_operation(
                    operation,
                    inherited_body=body_params,
                    global_consumes=global_consumes,
                    global_produces=global_produces,
                    shared_parameters=shared_parameters,
                    warnings=warnings,
                    context=f"{method.upper()} {path}",
                )
        paths[path] = converted_item
    return paths


def _convert_operation(
    operation: dict,
    *,
    inherited_body: list[dict],
    global_consumes: list[str],
    global_produces: list[str],
    shared_parameters: dict | None,
    warnings: list[SpecWarning],
    context: str,
) -> dict:
    converted: dict[str, Any] = {}
    for key in ("tags", "summary", "description", "operationId", "deprecated", "security"):
        if key in operation:
            converted[key] = operation[key]
    if isinstance(operation.get("externalDocs"), dict):
        converted["externalDocs"] = operation["externalDocs"]
    for key, value in operation.items():
        if key.startswith("x-"):
            converted[key] = value

    if operation.get("schemes"):
        warnings.append(
            SpecWarning(
                code="swagger2_operation_schemes",
                message=(
                    "Operation-level `schemes` have no OpenAPI 3 equivalent below the server "
                    "level and were dropped; the document-level server URL applies."
                ),
                context=context,
            )
        )

    consumes = _string_list(operation.get("consumes")) or global_consumes
    produces = _string_list(operation.get("produces")) or global_produces

    params, body_params = _split_parameters(
        operation.get("parameters") or [], shared_parameters, warnings, context
    )
    if params:
        converted["parameters"] = params

    body_params = list(inherited_body) + list(body_params)
    request_body = _request_body(body_params, consumes, warnings, context)
    if request_body:
        converted["requestBody"] = request_body

    converted["responses"] = {
        str(status): _convert_response(response, produces, warnings, f"{context} {status}")
        for status, response in (operation.get("responses") or {}).items()
        if isinstance(response, dict)
    }
    return converted


def _split_parameters(
    parameters: list,
    shared_parameters: dict | None,
    warnings: list[SpecWarning],
    context: str,
) -> tuple[list[dict], list[dict]]:
    """Return (non-body parameters, body/formData parameters)."""
    regular: list[dict] = []
    body: list[dict] = []
    for param in parameters:
        if not isinstance(param, dict):
            continue
        if "$ref" in param:
            # A reference may point at a shared body parameter, which OAS3
            # cannot express; those were skipped when building components, so
            # the reference is kept and will surface as an unresolved ref.
            regular.append(param)
            continue
        if param.get("in") in ("body", "formData"):
            body.append(param)
        else:
            regular.append(_convert_parameter(param, warnings, context))
    return regular, body


def _convert_parameter(param: dict, warnings: list[SpecWarning], context: str) -> dict:
    converted: dict[str, Any] = {
        "name": param.get("name"),
        "in": param.get("in"),
    }
    if param.get("description"):
        converted["description"] = param["description"]
    if param.get("in") == "path":
        converted["required"] = True
    elif "required" in param:
        converted["required"] = bool(param["required"])
    if param.get("allowEmptyValue") is not None:
        converted["allowEmptyValue"] = param["allowEmptyValue"]

    schema = {key: param[key] for key in _SCHEMA_KEYS if key in param}
    if schema.get("type") == "file":
        schema = {"type": "string", "format": "binary"}
    if "items" in schema:
        schema["items"] = _convert_schema(schema["items"], warnings, context)
    converted["schema"] = schema or {"type": "string"}

    collection_format = param.get("collectionFormat")
    if collection_format:
        style, explode = _COLLECTION_FORMATS.get(collection_format, (None, None))
        if style is None:
            warnings.append(
                SpecWarning(
                    code="swagger2_collection_format",
                    message=(
                        f"collectionFormat '{collection_format}' on parameter "
                        f"'{param.get('name')}' has no OpenAPI 3 equivalent; the default "
                        "serialization is used."
                    ),
                    context=context,
                )
            )
        else:
            converted["style"] = style
            converted["explode"] = explode

    for key, value in param.items():
        if key.startswith("x-"):
            converted[key] = value
    return converted


def _request_body(
    body_params: list[dict], consumes: list[str], warnings: list[SpecWarning], context: str
) -> dict | None:
    if not body_params:
        return None

    schema_params = [p for p in body_params if p.get("in") == "body"]
    form_params = [p for p in body_params if p.get("in") == "formData"]

    if schema_params and form_params:
        warnings.append(
            SpecWarning(
                code="swagger2_body_and_formdata",
                message=(
                    "The operation declares both a body parameter and formData parameters, "
                    "which Swagger 2 does not permit. The body parameter was used."
                ),
                context=context,
            )
        )
        form_params = []

    if len(schema_params) > 1:
        warnings.append(
            SpecWarning(
                code="swagger2_multiple_body_parameters",
                message=(
                    "More than one body parameter was declared, which Swagger 2 does not "
                    "permit. The first was used."
                ),
                context=context,
            )
        )

    if schema_params:
        param = schema_params[0]
        schema = _convert_schema(param.get("schema") or {}, warnings, context)
        media_types = [ct for ct in consumes if ct] or ["application/json"]
        body: dict[str, Any] = {
            "content": {media_type: {"schema": schema} for media_type in media_types}
        }
        if param.get("description"):
            body["description"] = param["description"]
        if param.get("required"):
            body["required"] = True
        return body

    # formData: build an object schema from the individual fields.
    properties: dict[str, Any] = {}
    required: list[str] = []
    encoding: dict[str, Any] = {}
    has_file = False
    for param in form_params:
        name = param.get("name")
        if not name:
            continue
        field = {key: param[key] for key in _SCHEMA_KEYS if key in param}
        if field.get("type") == "file":
            field = {"type": "string", "format": "binary"}
            has_file = True
        if param.get("description"):
            field["description"] = param["description"]
        if "items" in field:
            field["items"] = _convert_schema(field["items"], warnings, context)
        properties[name] = field
        if param.get("required"):
            required.append(name)
        if param.get("collectionFormat") == "multi":
            encoding[name] = {"style": "form", "explode": True}

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    declared = [
        ct for ct in consumes if ct in ("application/x-www-form-urlencoded", "multipart/form-data")
    ]
    media_type = (
        declared[0]
        if declared
        else ("multipart/form-data" if has_file else "application/x-www-form-urlencoded")
    )
    media: dict[str, Any] = {"schema": schema}
    if encoding:
        media["encoding"] = encoding
    return {"content": {media_type: media}, "required": bool(required)}


def _convert_response(
    response: dict, produces: list[str], warnings: list[SpecWarning], context: str
) -> dict:
    converted: dict[str, Any] = {"description": response.get("description") or ""}
    schema = response.get("schema")
    if isinstance(schema, dict):
        if schema.get("type") == "file":
            converted["content"] = {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        else:
            converted_schema = _convert_schema(schema, warnings, context)
            media_types = [ct for ct in produces if ct] or ["application/json"]
            converted["content"] = {ct: {"schema": converted_schema} for ct in media_types}

    headers = response.get("headers")
    if isinstance(headers, dict):
        converted["headers"] = {}
        for name, header in headers.items():
            if not isinstance(header, dict):
                continue
            header_schema = {key: header[key] for key in _SCHEMA_KEYS if key in header}
            entry: dict[str, Any] = {"schema": header_schema or {"type": "string"}}
            if header.get("description"):
                entry["description"] = header["description"]
            converted["headers"][name] = entry

    examples = response.get("examples")
    if isinstance(examples, dict) and "content" in converted:
        for media_type, example in examples.items():
            if media_type in converted["content"]:
                converted["content"][media_type]["example"] = example

    for key, value in response.items():
        if key.startswith("x-"):
            converted[key] = value
    return converted


# ── schemas ──────────────────────────────────────────────────────────────────


def _convert_schema(schema: Any, warnings: list[SpecWarning], context: str) -> Any:
    """Rewrite a Swagger 2 schema into a JSON Schema OAS3 accepts."""
    if isinstance(schema, list):
        return [_convert_schema(item, warnings, context) for item in schema]
    if not isinstance(schema, dict):
        return schema

    converted: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and value == "file":
            converted["type"] = "string"
            converted["format"] = "binary"
            continue
        if key == "x-nullable":
            converted["nullable"] = bool(value)
            continue
        if key in ("properties", "patternProperties", "definitions"):
            converted[key] = {
                name: _convert_schema(sub, warnings, context) for name, sub in (value or {}).items()
            }
            continue
        if key in ("items", "additionalProperties", "not"):
            converted[key] = _convert_schema(value, warnings, context)
            continue
        if key in ("allOf", "anyOf", "oneOf"):
            converted[key] = [_convert_schema(sub, warnings, context) for sub in (value or [])]
            continue
        if key == "discriminator" and isinstance(value, str):
            # Swagger 2's discriminator is a bare property name.
            converted["discriminator"] = {"propertyName": value}
            continue
        converted[key] = value
    return converted


def _rewrite_refs(node: Any) -> Any:
    """Point every internal `$ref` at its OpenAPI 3 location."""
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                for old, new in _REF_MAP:
                    if value.startswith(old):
                        value = new + value[len(old) :]
                        break
                result[key] = value
            else:
                result[key] = _rewrite_refs(value)
        return result
    if isinstance(node, list):
        return [_rewrite_refs(item) for item in node]
    return node


# ── security ─────────────────────────────────────────────────────────────────


def _security_schemes(source: dict, warnings: list[SpecWarning]) -> dict:
    definitions = source.get("securityDefinitions")
    if not isinstance(definitions, dict):
        return {}

    schemes: dict[str, Any] = {}
    for name, definition in definitions.items():
        if not isinstance(definition, dict):
            continue
        kind = definition.get("type")
        if kind == "basic":
            scheme: dict[str, Any] = {"type": "http", "scheme": "basic"}
        elif kind == "apiKey":
            scheme = {"type": "apiKey", "name": definition.get("name"), "in": definition.get("in")}
        elif kind == "oauth2":
            scheme = _oauth2_scheme(name, definition, warnings)
        else:
            warnings.append(
                SpecWarning(
                    code="swagger2_unknown_security_type",
                    message=(
                        f"Security definition '{name}' has type '{kind}', which Swagger 2 does "
                        "not define. It was skipped."
                    ),
                    context=f"securityDefinitions.{name}",
                )
            )
            continue
        if definition.get("description"):
            scheme["description"] = definition["description"]
        schemes[name] = scheme
    return schemes


def _oauth2_scheme(name: str, definition: dict, warnings: list[SpecWarning]) -> dict:
    swagger_flow = definition.get("flow")
    oas_flow = _OAUTH_FLOW_NAMES.get(str(swagger_flow))
    if oas_flow is None:
        warnings.append(
            SpecWarning(
                code="swagger2_unknown_oauth_flow",
                message=(
                    f"OAuth2 definition '{name}' declares flow '{swagger_flow}', which has no "
                    "OpenAPI 3 equivalent. The scheme was emitted with no flows."
                ),
                context=f"securityDefinitions.{name}",
            )
        )
        return {"type": "oauth2", "flows": {}}

    flow: dict[str, Any] = {"scopes": definition.get("scopes") or {}}
    if oas_flow in ("implicit", "authorizationCode") and definition.get("authorizationUrl"):
        flow["authorizationUrl"] = definition["authorizationUrl"]
    if oas_flow in ("password", "clientCredentials", "authorizationCode") and definition.get(
        "tokenUrl"
    ):
        flow["tokenUrl"] = definition["tokenUrl"]
    return {"type": "oauth2", "flows": {oas_flow: flow}}


# ── helpers ──────────────────────────────────────────────────────────────────


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


_MEDIA_TYPE_RE = re.compile(r"^[\w.+-]+/[\w.+*-]+")


def looks_like_media_type(value: str) -> bool:
    return bool(_MEDIA_TYPE_RE.match(value or ""))
