"""Validation and normalization of a parsed OpenAPI document into the IR.

Pipeline position (Sutr spec §17/§19):

    parsed dict → validate (openapi-spec-validator) → resolve $refs →
    ApiDefinition IR (servers, security schemes, operations)

The IR carries everything needed to compile MCP tools and execute requests
without re-reading the original document.
"""

import re
from typing import Any

from pydantic import BaseModel

from sutr.openapi.errors import OpenAPIError, SpecWarning
from sutr.openapi.fingerprint import IR_VERSION
from sutr.openapi.limits import MAX_OPERATIONS
from sutr.openapi.linting import LintFinding, lint_document
from sutr.openapi.linting.schema_errors import schema_findings
from sutr.openapi.resolver import resolve_refs
from sutr.openapi.swagger2 import convert as convert_swagger2
from sutr.openapi.swagger2 import is_swagger2

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
    type: str  # apiKey | http | oauth2 | openIdConnect | mutualTLS
    scheme: str | None = None  # for http: bearer | basic | ...
    param_name: str | None = None  # for apiKey: the header/query/cookie name
    location: str | None = None  # for apiKey: header | query | cookie
    description: str | None = None
    # for oauth2: the declared grants, kept intact rather than flattened, so
    # the platform can actually run one (build prompt §24, ADR-009).
    flows: dict = {}
    # for openIdConnect: the discovery document URL.
    openid_connect_url: str | None = None


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
    # How the body is encoded on the wire: json | form | multipart | binary |
    # text. Derived from the media type, and carried all the way through to
    # both runtimes (build prompt §26 — the content type must be preserved,
    # not collapsed to JSON).
    encoding: str = "json"


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
    warnings: list[SpecWarning] = []
    # Advisory findings from the Spectral-style rule layer. They never block an
    # import — structural validity is the gate — but they predict how good the
    # generated tools will be, so they travel with the definition.
    lint_findings: list[LintFinding] = []
    # "openapi3" or "swagger2" — which dialect the user actually supplied.
    # A Swagger 2.0 document is converted before anything else sees it, so
    # this is the only place the original dialect survives in the IR.
    source_dialect: str = "openapi3"
    # Which compiler shape produced this IR. An IR carried forward from an
    # older build is recognisable rather than silently mixed in (build
    # prompt §14). See `openapi/fingerprint.py`.
    ir_version: int = IR_VERSION


def validate_spec(document: dict) -> None:
    """Structural validation against the official OpenAPI meta-schemas.

    Raises with *every* violation attached (build prompt §16), not just the
    first: fixing a large specification one error per round-trip is the
    behaviour this replaces.
    """
    version = document.get("openapi") or document.get("swagger")
    if not isinstance(version, str):
        raise OpenAPIError(
            "missing_version", "The document does not declare an 'openapi' version field."
        )
    if version.startswith("2."):
        # `normalize()` converts Swagger 2.0 before validating, so reaching
        # here means a caller validated a raw 2.0 document directly.
        raise OpenAPIError(
            "unsupported_version",
            "This is a Swagger 2.0 document. Import it through `normalize()`, which converts "
            "it to OpenAPI 3.x first.",
        )
    if not (version.startswith("3.0") or version.startswith("3.1")):
        raise OpenAPIError(
            "unsupported_version", f"Unsupported OpenAPI version '{version}' (need 3.0.x or 3.1.x)."
        )
    findings = schema_findings(document, version)
    if findings:
        summary = findings[0].message
        count = len(findings)
        suffix = f" (and {count - 1} more)" if count > 1 else ""
        raise OpenAPIError(
            "invalid_spec",
            f"OpenAPI validation failed: {summary}{suffix}",
            findings=findings,
        )


def _body_encoding(media_type: str, schema: dict) -> str | None:
    """Classify a media type into one of the runtime's body encodings.

    Returns None for a media type the runtime cannot construct, so the caller
    can warn instead of producing a tool that silently sends nothing.
    """
    base = media_type.split(";")[0].strip().lower()
    if base == "application/json" or base.endswith("+json"):
        return "json"
    if base == "application/x-www-form-urlencoded":
        return "form"
    if base == "multipart/form-data":
        return "multipart"
    if base == "application/octet-stream" or base.startswith("image/"):
        return "binary"
    if isinstance(schema, dict) and schema.get("format") in ("binary", "byte"):
        return "binary"
    if base.startswith("text/") or base in ("application/xml", "application/yaml"):
        return "text"
    return None


# Preference order when an operation offers several media types. JSON first
# because it maps most faithfully onto MCP's JSON arguments; binary last
# because it forces base64 on the caller.
_ENCODING_PREFERENCE = ("json", "form", "multipart", "text", "binary")


def _pick_body_content(content: dict) -> tuple[str, dict, str] | None:
    """Choose the best-supported media type from a content map.

    Returns (media_type, schema, encoding), or None when nothing in the map
    can be constructed by the runtime.
    """
    candidates: list[tuple[int, str, dict, str]] = []
    for media_type, media in content.items():
        schema = (media or {}).get("schema") or {}
        encoding = _body_encoding(str(media_type), schema)
        if encoding is None:
            continue
        candidates.append((_ENCODING_PREFERENCE.index(encoding), str(media_type), schema, encoding))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, media_type, schema, encoding = candidates[0]
    return media_type, schema, encoding


def _security_requirement_names(requirements: Any) -> list[str]:
    """Flatten a security requirement array into the first alternative's scheme names."""
    if not isinstance(requirements, list) or not requirements:
        return []
    first = requirements[0]
    if isinstance(first, dict):
        return list(first.keys())
    return []


def normalize(document: dict) -> ApiDefinition:
    """Validate + dereference + normalize a parsed OpenAPI dict into the IR.

    A Swagger 2.0 document is converted to OpenAPI 3.0 first (build prompt
    §17), so every stage downstream of this function — validation, linting,
    `$ref` resolution, compilation, packaging — only ever sees 3.x.
    """
    dialect = "openapi3"
    conversion_warnings: list[SpecWarning] = []
    if is_swagger2(document):
        dialect = "swagger2"
        document, conversion_warnings = convert_swagger2(document)
    validate_spec(document)
    # Lint the *original* document: findings must point at the file the user
    # wrote, not at the dereferenced copy they have never seen.
    lint_findings = lint_document(document).findings
    resolved = resolve_refs(document)
    warnings: list[SpecWarning] = list(conversion_warnings)

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
                flows=scheme.get("flows") if isinstance(scheme.get("flows"), dict) else {},
                openid_connect_url=scheme.get("openIdConnectUrl"),
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
                picked = _pick_body_content(content)
                if picked:
                    content_type, schema, encoding = picked
                    body = NBody(
                        content_type=content_type,
                        json_schema=schema,
                        required=bool(request_body.get("required")),
                        description=request_body.get("description"),
                        encoding=encoding,
                    )
                    if len(content) > 1:
                        warnings.append(
                            SpecWarning(
                                code="body_media_type_selected",
                                message=(
                                    f"The operation offers {sorted(content.keys())}; "
                                    f"'{content_type}' was selected for the generated tool."
                                ),
                                context=f"{method.upper()} {path}",
                            )
                        )
                elif content:
                    warnings.append(
                        SpecWarning(
                            code="unsupported_body",
                            message=(
                                "Request body uses media type(s) "
                                f"{sorted(content.keys())}, none of which the runtime can "
                                "construct. The generated tool sends no body."
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
        lint_findings=lint_findings,
        source_dialect=dialect,
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
