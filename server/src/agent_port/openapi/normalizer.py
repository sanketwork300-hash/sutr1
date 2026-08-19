"""Validation and normalization of a parsed OpenAPI document into the IR.

Pipeline position (Sutr spec §17/§19):

    parsed dict → validate (openapi-spec-validator) → resolve $refs →
    ApiDefinition IR (servers, security schemes, operations)

The IR carries everything needed to compile MCP tools and execute requests
without re-reading the original document.
"""

import re
from typing import Any

from openapi_spec_validator import validate as validate_openapi
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from pydantic import BaseModel

from agent_port.openapi.errors import OpenAPIError, Warning_
from agent_port.openapi.limits import MAX_OPERATIONS
from agent_port.openapi.resolver import resolve_refs

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_SERVER_VAR_RE = re.compile(r"\{([^{}]+)\}")


class ServerVariable(BaseModel):
    default: str
    enum: list[str] | None = None
    description: str | None = None


class Server(BaseModel):
    url: str
    description: str | None = None
    variables: dict[str, ServerVariable] = {}


class SecurityScheme(BaseModel):
    name: str  # scheme name in components.securitySchemes
    type: str  # apiKey | http | oauth2 | openIdConnect
    scheme: str | None = None  # for http: bearer | basic | ...
    param_name: str | None = None  # for apiKey: the header/query/cookie name
    location: str | None = None  # for apiKey: header | query | cookie
    description: str | None = None


class NParam(BaseModel):
    name: str
    location: str  # path | query | header | cookie
    required: bool = False
    description: str | None = None
    json_schema: dict = {}


class NBody(BaseModel):
    content_type: str
    json_schema: dict = {}
    required: bool = False
    description: str | None = None


class Operation(BaseModel):
    operation_id: str | None = None
    method: str  # upper-case
    path: str
    summary: str | None = None
    description: str | None = None
    tags: list[str] = []
    deprecated: bool = False
    parameters: list[NParam] = []
    request_body: NBody | None = None
    # {status_code: description} — enough for tool-description hints.
    responses: dict[str, str] = {}
    # Security requirement scheme names for this operation (None → inherit global).
    security_schemes: list[str] | None = None


class ApiDefinition(BaseModel):
    title: str
    version: str
    description: str | None = None
    openapi_version: str
    servers: list[Server] = []
    security_schemes: list[SecurityScheme] = []
    global_security: list[str] = []  # scheme names required by default
    operations: list[Operation] = []
    warnings: list[Warning_] = []


def validate_spec(document: dict) -> None:
    """Structural validation against the official OpenAPI meta-schemas."""
    version = document.get("openapi") or document.get("swagger")
    if not isinstance(version, str):
        raise OpenAPIError(
            "missing_version", "The document does not declare an 'openapi' version field."
        )
    if version.startswith("2."):
        raise OpenAPIError(
            "unsupported_version",
            "Swagger 2.0 is not supported — convert the document to OpenAPI 3.x first.",
        )
    if not (version.startswith("3.0") or version.startswith("3.1")):
        raise OpenAPIError(
            "unsupported_version", f"Unsupported OpenAPI version '{version}' (need 3.0.x or 3.1.x)."
        )
    try:
        validate_openapi(document)
    except OpenAPIValidationError as exc:
        # First line of the validator message is the actual problem.
        message = str(exc).split("\n")[0][:500]
        raise OpenAPIError("invalid_spec", f"OpenAPI validation failed: {message}")


def _pick_json_content(content: dict) -> tuple[str, dict] | None:
    """Choose the JSON-ish media type from a content map."""
    for ct, media in content.items():
        base = ct.split(";")[0].strip().lower()
        if base == "application/json" or base.endswith("+json"):
            return ct, (media or {}).get("schema") or {}
    return None


def _security_requirement_names(requirements: Any) -> list[str]:
    """Flatten a security requirement array into the first alternative's scheme names."""
    if not isinstance(requirements, list) or not requirements:
        return []
    first = requirements[0]
    if isinstance(first, dict):
        return list(first.keys())
    return []


def normalize(document: dict) -> ApiDefinition:
    """Validate + dereference + normalize a parsed OpenAPI dict into the IR."""
    validate_spec(document)
    resolved = resolve_refs(document)
    warnings: list[Warning_] = []

    info = resolved.get("info") or {}
    servers = []
    for server in resolved.get("servers") or []:
        if not isinstance(server, dict) or not server.get("url"):
            continue
        variables = {}
        for var_name, var in (server.get("variables") or {}).items():
            if isinstance(var, dict) and "default" in var:
                variables[var_name] = ServerVariable(
                    default=str(var["default"]),
                    enum=[str(v) for v in var["enum"]] if var.get("enum") else None,
                    description=var.get("description"),
                )
        servers.append(
            Server(url=server["url"], description=server.get("description"), variables=variables)
        )

    schemes: list[SecurityScheme] = []
    for name, scheme in ((resolved.get("components") or {}).get("securitySchemes") or {}).items():
        if not isinstance(scheme, dict):
            continue
        schemes.append(
            SecurityScheme(
                name=name,
                type=scheme.get("type", ""),
                scheme=(scheme.get("scheme") or None),
                param_name=scheme.get("name"),
                location=scheme.get("in"),
                description=scheme.get("description"),
            )
        )

    operations: list[Operation] = []
    paths = resolved.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters") or []
        for method in _HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            if len(operations) >= MAX_OPERATIONS:
                raise OpenAPIError(
                    "too_many_operations",
                    f"The specification has more than {MAX_OPERATIONS} operations. "
                    "Split it or reduce it before importing.",
                )

            # Path-item parameters are shared by all operations; an operation-
            # level parameter with the same (name, in) overrides the shared one.
            merged: dict[tuple[str, str], NParam] = {}
            for raw in list(shared_params) + list(op.get("parameters") or []):
                if isinstance(raw, dict) and raw.get("name") and raw.get("in"):
                    merged[(raw["name"], raw["in"])] = NParam(
                        name=raw["name"],
                        location=raw["in"],
                        required=bool(raw.get("required", raw["in"] == "path")),
                        description=raw.get("description"),
                        json_schema=raw.get("schema") or {},
                    )
            params = list(merged.values())

            body: NBody | None = None
            request_body = op.get("requestBody")
            if isinstance(request_body, dict):
                content = request_body.get("content") or {}
                picked = _pick_json_content(content)
                if picked:
                    content_type, schema = picked
                    body = NBody(
                        content_type=content_type,
                        json_schema=schema,
                        required=bool(request_body.get("required")),
                        description=request_body.get("description"),
                    )
                elif content:
                    warnings.append(
                        Warning_(
                            code="unsupported_body",
                            message=(
                                "Request body uses unsupported media type(s) "
                                f"{sorted(content.keys())} — only JSON bodies are compiled."
                            ),
                            context=f"{method.upper()} {path}",
                        )
                    )

            responses: dict[str, str] = {}
            for status, resp in (op.get("responses") or {}).items():
                if isinstance(resp, dict) and resp.get("description"):
                    responses[str(status)] = str(resp["description"])

            op_security = op.get("security")
            operations.append(
                Operation(
                    operation_id=op.get("operationId"),
                    method=method.upper(),
                    path=path,
                    summary=op.get("summary"),
                    description=op.get("description"),
                    tags=[str(t) for t in op.get("tags") or []],
                    deprecated=bool(op.get("deprecated")),
                    parameters=params,
                    request_body=body,
                    responses=responses,
                    security_schemes=(
                        _security_requirement_names(op_security)
                        if op_security is not None
                        else None
                    ),
                )
            )

    if not operations:
        raise OpenAPIError("no_operations", "The specification defines no operations.")

    return ApiDefinition(
        title=str(info.get("title") or "Imported API"),
        version=str(info.get("version") or "0.0.0"),
        description=info.get("description"),
        openapi_version=str(resolved.get("openapi")),
        servers=servers,
        security_schemes=schemes,
        global_security=_security_requirement_names(resolved.get("security")),
        operations=operations,
        warnings=warnings,
    )


def substitute_server_url(server: Server, overrides: dict[str, str] | None = None) -> str:
    """Apply server variables (defaults + user overrides) to the server URL (§22)."""
    overrides = overrides or {}
    url = server.url

    def replace(match: re.Match) -> str:
        var_name = match.group(1)
        variable = server.variables.get(var_name)
        value = overrides.get(var_name, variable.default if variable else None)
        if value is None:
            raise OpenAPIError(
                "unresolved_server_variable",
                f"Server variable '{var_name}' has no default and no value was provided.",
            )
        if variable and variable.enum and value not in variable.enum:
            raise OpenAPIError(
                "invalid_server_variable",
                f"Server variable '{var_name}' must be one of {variable.enum}.",
            )
        return value

    return _SERVER_VAR_RE.sub(replace, url)
