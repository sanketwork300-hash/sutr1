"""Compile the normalized IR into declarative MCP tools (ApiTool records).

Responsibilities (Sutr spec §20, §23–§25):
- endpoint/tag/operation filtering before generation
- deterministic tool naming from operationId (or method+path), with
  documented, deterministic collision handling that fails loudly when unique
  safe names cannot be produced
- parameter mapping: path/query/header params + request bodies of every
  supported encoding — JSON, form-urlencoded, multipart (with binary parts),
  binary, and text (object bodies are flattened into top-level params;
  non-object bodies become a single wrapped parameter)
- honest, LLM-oriented descriptions assembled only from what the spec states
"""

import re

from pydantic import BaseModel

from sutr.integrations.types import ApiTool, Param
from sutr.openapi.errors import OpenAPIError, SpecWarning
from sutr.openapi.limits import (
    MAX_GENERATED_TOOLS,
    MAX_PARAMS_PER_TOOL,
    MAX_SCHEMA_DEPTH,
)
from sutr.openapi.normalizer import ApiDefinition, NParam, Operation

_TOOL_NAME_RE = re.compile(r"[^a-z0-9_]+")
_PARAM_NAME_RE = re.compile(r"[^a-zA-Z0-9_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOOL_NAME_MAX = 64


class CompileFilters(BaseModel):
    include_tags: list[str] = []
    exclude_tags: list[str] = []
    include_paths: list[str] = []  # exact path or prefix ending with '*'
    exclude_paths: list[str] = []
    include_operations: list[str] = []  # operationIds
    exclude_operations: list[str] = []
    include_deprecated: bool = False


class CompiledTool(BaseModel):
    tool: ApiTool
    operation_id: str | None
    method: str
    path: str
    tags: list[str] = []
    renamed_from: str | None = None  # set when collision handling changed the name


class CompileResult(BaseModel):
    tools: list[CompiledTool]
    warnings: list[SpecWarning] = []


def _matches_path(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("*"):
            if path.startswith(pattern[:-1]):
                return True
        elif path == pattern:
            return True
    return False


def filter_operations(
    definition: ApiDefinition, filters: CompileFilters
) -> tuple[list[Operation], list[SpecWarning]]:
    warnings: list[SpecWarning] = []
    selected: list[Operation] = []
    for op in definition.operations:
        if op.deprecated and not filters.include_deprecated:
            continue
        if filters.include_tags and not (set(op.tags) & set(filters.include_tags)):
            continue
        if filters.exclude_tags and (set(op.tags) & set(filters.exclude_tags)):
            continue
        if filters.include_paths and not _matches_path(op.path, filters.include_paths):
            continue
        if filters.exclude_paths and _matches_path(op.path, filters.exclude_paths):
            continue
        if filters.include_operations and op.operation_id not in filters.include_operations:
            continue
        if filters.exclude_operations and op.operation_id in filters.exclude_operations:
            continue
        selected.append(op)
    if not selected:
        raise OpenAPIError("nothing_selected", "The filters exclude every operation.")
    return selected, warnings


# ── Naming ───────────────────────────────────────────────────────────────────


def _snake(text: str) -> str:
    return _TOOL_NAME_RE.sub("_", _CAMEL_RE.sub("_", text).lower()).strip("_")


def _lead_alpha(name: str) -> str:
    """Tool names must start with a letter (custom-API contract)."""
    return name if name and name[0].isalpha() else f"op_{name}" if name else name


def _safe_param(name: str) -> str:
    safe = _PARAM_NAME_RE.sub("_", name)
    if not safe or not (safe[0].isalpha() or safe[0] == "_"):
        safe = f"_{safe}"
    return safe


def _base_tool_name(op: Operation) -> str:
    if op.operation_id:
        name = _lead_alpha(_snake(op.operation_id))
        if name:
            return name[:_TOOL_NAME_MAX]
    segments = [s for s in op.path.split("/") if s and not s.startswith("{")]
    slug = "_".join(_snake(s) for s in segments) or "root"
    return f"{op.method.lower()}_{slug}"[:_TOOL_NAME_MAX]


def _path_qualifier(op: Operation) -> str:
    """A deterministic disambiguator built from the full path incl. params."""
    segments = []
    for s in op.path.split("/"):
        if not s:
            continue
        segments.append("by_" + _snake(s[1:-1]) if s.startswith("{") else _snake(s))
    return "_".join(segments) or "root"


def assign_tool_names(operations: list[Operation]) -> dict[int, tuple[str, str | None]]:
    """Map operation index → (final_name, renamed_from).

    Strategy (deterministic, documented):
    1. Base name from snake_cased operationId, else `<method>_<path-slug>`.
    2. On collision, every colliding operation is renamed to
       `<method>_<full-path-qualifier>` (path params become `by_<name>`).
    3. Remaining collisions get numeric suffixes `_2`, `_3`, … in spec order.
    Fails loudly if a unique non-empty name still cannot be produced.
    """
    base_names: dict[int, str] = {i: _base_tool_name(op) for i, op in enumerate(operations)}
    counts: dict[str, int] = {}
    for name in base_names.values():
        counts[name] = counts.get(name, 0) + 1

    final: dict[int, tuple[str, str | None]] = {}
    used: set[str] = set()
    for i, op in enumerate(operations):
        base = base_names[i]
        if counts[base] == 1 and base not in used:
            final[i] = (base, None)
            used.add(base)
            continue
        qualified = f"{op.method.lower()}_{_path_qualifier(op)}"[:_TOOL_NAME_MAX]
        candidate = qualified
        suffix = 2
        while candidate in used or not candidate:
            candidate = f"{qualified}_{suffix}"[:_TOOL_NAME_MAX]
            suffix += 1
            if suffix > 1000:
                raise OpenAPIError(
                    "naming_failed",
                    f"Could not produce a unique tool name for {op.method} {op.path}.",
                )
        final[i] = (candidate, base if candidate != base else None)
        used.add(candidate)
    return final


# ── Schema → Param mapping ───────────────────────────────────────────────────


def _schema_depth(schema, depth: int = 0) -> int:
    if depth > MAX_SCHEMA_DEPTH:
        return depth
    if isinstance(schema, dict):
        return max(
            [depth] + [_schema_depth(v, depth + 1) for v in schema.values()],
            default=depth,
        )
    if isinstance(schema, list):
        return max([depth] + [_schema_depth(v, depth + 1) for v in schema], default=depth)
    return depth


def _clean_schema(schema: dict) -> dict:
    """Drop OpenAPI-only keys that aren't valid/useful in tool input schemas."""
    if not isinstance(schema, dict):
        return {}
    cleaned = {k: v for k, v in schema.items() if not k.startswith("x-") and k != "xml"}
    return cleaned


def _param_from_nparam(np: NParam, warnings: list[SpecWarning], context: str) -> Param | None:
    if np.location == "cookie":
        warnings.append(
            SpecWarning(
                code="cookie_param_skipped",
                message=f"Cookie parameter '{np.name}' is not supported and was skipped.",
                context=context,
            )
        )
        return None
    safe_name = _safe_param(np.name)
    schema = _clean_schema(np.json_schema)
    simple_type = schema.get("type") if isinstance(schema.get("type"), str) else None
    needs_override = bool(schema) and (
        set(schema) - {"type", "description", "default", "enum", "items"}
        or (schema.get("items") or {}).get("type") not in (None, *_SIMPLE_TYPES)
        or (simple_type not in _SIMPLE_TYPES if simple_type else True)
    )
    wire_name = np.name if safe_name != np.name else None
    if needs_override:
        override = dict(schema)
        if np.description and "description" not in override:
            override["description"] = np.description
        return Param(
            name=safe_name,
            required=np.required,
            description=np.description,
            schema_override=override,
            location=np.location,
            wire_name=wire_name,
        )
    return Param(
        name=safe_name,
        type=simple_type or "string",
        description=np.description or schema.get("description"),
        required=np.required,
        default=schema.get("default"),
        enum=[str(e) for e in schema["enum"]] if schema.get("enum") else None,
        items=(schema.get("items") or {}).get("type") if simple_type == "array" else None,
        location=np.location,
        wire_name=wire_name,
    )


_SIMPLE_TYPES = ("string", "number", "integer", "boolean", "array")


_BASE64_NOTE = (
    "Binary content: supply it as a base64-encoded string (MCP tool arguments "
    "are JSON, so raw bytes cannot be sent directly)."
)


def _binary_body_param(
    op: Operation, taken_names: set[str], *, text: bool
) -> tuple[list[Param], str]:
    """A whole-body parameter for a binary or text request body."""
    wrapper = _wrapper_name(taken_names)
    if text:
        description = (
            op.request_body.description
            or f"Raw request body sent as {op.request_body.content_type}."
        )
        override = {"type": "string", "description": description}
    else:
        description = f"{op.request_body.description or 'Request body.'} {_BASE64_NOTE}".strip()
        override = {"type": "string", "format": "binary", "description": description}
    return (
        [
            Param(
                name=wrapper,
                required=op.request_body.required,
                description=description,
                schema_override=override,
                location="body",
            )
        ],
        wrapper,
    )


def _body_params(
    op: Operation, taken_names: set[str], warnings: list[SpecWarning]
) -> tuple[list[Param], str | None]:
    """Map the request body to params, according to its encoding.

    - `json`, `form`, `multipart`: an object body flattens its top-level
      properties into individual params (a name collision with a path, query,
      or header param is reported and skipped, because the argument name *is*
      the wire key and a rename would send the wrong one).
    - `binary`, `text`, and non-object JSON bodies: one wrapped parameter,
      recorded on the tool as `body_param`.
    """
    if op.request_body is None:
        return [], None
    encoding = op.request_body.encoding
    schema = _clean_schema(op.request_body.json_schema)
    context = f"{op.method} {op.path}"

    if encoding == "binary":
        return _binary_body_param(op, taken_names, text=False)
    if encoding == "text":
        return _binary_body_param(op, taken_names, text=True)

    if encoding in ("form", "multipart") and not (
        schema.get("type") == "object" or "properties" in schema
    ):
        # A form body that is not an object has no field names to map, so
        # there is nothing honest to generate.
        warnings.append(
            SpecWarning(
                code="unsupported_body",
                message=(
                    f"The {op.request_body.content_type} request body declares no object "
                    "schema, so no form fields could be derived. The generated tool sends "
                    "no body."
                ),
                context=context,
            )
        )
        return [], None

    if schema.get("type") == "object" or ("properties" in schema and "type" not in schema):
        required_names = set(schema.get("required") or [])
        params: list[Param] = []
        for prop_name, prop_schema in (schema.get("properties") or {}).items():
            safe_name = _safe_param(prop_name)
            if safe_name in taken_names:
                # A rename would require a schema-level alias the runtime lacks
                # (the arg name IS the body key) — skip rather than send a
                # wrong key, and tell the user.
                warnings.append(
                    SpecWarning(
                        code="body_param_skipped",
                        message=(
                            f"Body property '{prop_name}' collides with a path/query/header "
                            "parameter of the same name and was skipped."
                        ),
                        context=context,
                    )
                )
                continue
            cleaned = _clean_schema(prop_schema if isinstance(prop_schema, dict) else {})
            if encoding == "multipart" and cleaned.get("format") in ("binary", "byte"):
                # A file field: the runtime uploads it as a part, and the
                # argument carries base64 because MCP arguments are JSON.
                description = f"{cleaned.get('description') or prop_name}. {_BASE64_NOTE}".strip()
                params.append(
                    Param(
                        name=safe_name,
                        required=prop_name in required_names,
                        description=description,
                        schema_override={
                            "type": "string",
                            "format": "binary",
                            "description": description,
                        },
                        location="body",
                        wire_name=prop_name if safe_name != prop_name else None,
                    )
                )
                continue
            params.append(
                _param_from_nparam(
                    NParam(
                        name=prop_name,
                        location="body",
                        required=prop_name in required_names,
                        description=cleaned.get("description"),
                        json_schema=cleaned,
                    ),
                    warnings,
                    context,
                )
            )
        if schema.get("additionalProperties") and not params:
            if encoding != "json":
                # A form body has named fields; a free-form object has none, so
                # there is nothing to name and nothing honest to generate.
                warnings.append(
                    SpecWarning(
                        code="unsupported_body",
                        message=(
                            f"The {op.request_body.content_type} request body is a free-form "
                            "object with no declared properties, so no form fields could be "
                            "derived. The generated tool sends no body."
                        ),
                        context=context,
                    )
                )
                return [], None
            wrapper = _wrapper_name(taken_names)
            # Free-form object: accept a single wrapped body argument.
            return (
                [
                    Param(
                        name=wrapper,
                        required=op.request_body.required,
                        description=op.request_body.description or "JSON request body object.",
                        schema_override={"type": "object"},
                        location="body",
                    )
                ],
                wrapper,
            )
        params = [p for p in params if p is not None]
        if encoding in ("form", "multipart") and not params:
            warnings.append(
                SpecWarning(
                    code="unsupported_body",
                    message=(
                        f"The {op.request_body.content_type} request body declares no "
                        "properties, so no form fields could be derived. The generated tool "
                        "sends no body."
                    ),
                    context=context,
                )
            )
            return [], None
        return params, None

    # Non-object body (array, string, …): wrap it in a single argument.
    wrapper = _wrapper_name(taken_names)
    description = op.request_body.description or "Raw JSON request body."
    override = schema or {"type": "object"}
    if "description" not in override:
        override = {**override, "description": description}
    return (
        [
            Param(
                name=wrapper,
                required=op.request_body.required,
                description=description,
                schema_override=override,
                location="body",
            )
        ],
        wrapper,
    )


def _wrapper_name(taken_names: set[str]) -> str:
    for candidate in ("body", "request_body", "payload"):
        if candidate not in taken_names:
            return candidate
    return "body_payload"


def _tool_description(op: Operation) -> str:
    """Assemble a factual description: summary, details, and success response.

    Never invents behaviour — every sentence comes from the specification.
    """
    parts: list[str] = []
    summary = (op.summary or "").strip()
    description = (op.description or "").strip()
    if summary:
        parts.append(summary.rstrip(".") + ".")
    if description and description != summary:
        parts.append(description)
    parts.append(f"HTTP {op.method} {op.path}.")
    success = next(
        (desc for status, desc in sorted(op.responses.items()) if status.startswith("2")),
        None,
    )
    if success:
        parts.append(f"Returns: {success.rstrip('.')}.")
    if op.deprecated:
        parts.append("DEPRECATED in the source API.")
    text = " ".join(parts)
    return text[:2000]


def compile_definition(
    definition: ApiDefinition, filters: CompileFilters | None = None
) -> CompileResult:
    filters = filters or CompileFilters()
    operations, warnings = filter_operations(definition, filters)

    if len(operations) > MAX_GENERATED_TOOLS:
        raise OpenAPIError(
            "too_many_tools",
            f"The selection would generate {len(operations)} tools "
            f"(maximum {MAX_GENERATED_TOOLS}). Narrow the filters.",
        )

    names = assign_tool_names(operations)
    compiled: list[CompiledTool] = []
    for i, op in enumerate(operations):
        name, renamed_from = names[i]
        context = f"{op.method} {op.path}"

        params: list[Param] = []
        for np in op.parameters:
            if _schema_depth(np.json_schema) > MAX_SCHEMA_DEPTH:
                raise OpenAPIError(
                    "schema_too_deep", f"A parameter schema in {context} is nested too deeply."
                )
            p = _param_from_nparam(np, warnings, context)
            if p is not None:
                params.append(p)

        if op.request_body and _schema_depth(op.request_body.json_schema) > MAX_SCHEMA_DEPTH:
            raise OpenAPIError(
                "schema_too_deep", f"The request body schema in {context} is nested too deeply."
            )
        taken = {p.name for p in params}
        body_params, body_param_name = _body_params(op, taken, warnings)
        params.extend(p for p in body_params if p is not None)

        if len(params) > MAX_PARAMS_PER_TOOL:
            raise OpenAPIError(
                "too_many_params",
                f"{context} would generate {len(params)} parameters "
                f"(maximum {MAX_PARAMS_PER_TOOL}).",
            )

        # Path placeholders must match the sanitized argument names so the
        # runtime's {param} substitution finds them.
        tool_path = op.path
        for np in op.parameters:
            if np.location == "path":
                safe = _safe_param(np.name)
                if safe != np.name:
                    tool_path = tool_path.replace("{" + np.name + "}", "{" + safe + "}")

        compiled.append(
            CompiledTool(
                tool=ApiTool(
                    name=name,
                    description=_tool_description(op),
                    method=op.method,
                    path=tool_path,
                    params=params,
                    body_param=body_param_name,
                    body_encoding=(op.request_body.encoding if op.request_body else "none"),
                    body_content_type=(op.request_body.content_type if op.request_body else None),
                ),
                operation_id=op.operation_id,
                method=op.method,
                path=op.path,
                tags=op.tags,
                renamed_from=renamed_from,
            )
        )

    return CompileResult(tools=compiled, warnings=warnings)
