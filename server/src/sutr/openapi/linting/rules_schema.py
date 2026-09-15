"""Schema and parameter rules.

The generated tool's input schema is derived directly from these, so a sloppy
schema here becomes a tool an agent calls wrongly.
"""

from sutr.openapi.linting.finding import LintFinding, Severity, pointer
from sutr.openapi.linting.registry import rule
from sutr.openapi.linting.walk import effective_parameters, iter_operations

DOC = "/openapi-linting#"

_SCALARS = ("string", "number", "integer", "boolean")


def _finding(rule_id, severity, message, location, json_pointer, remediation) -> LintFinding:
    return LintFinding(
        rule_id=rule_id,
        severity=severity,
        message=message,
        location=location,
        json_pointer=json_pointer,
        documentation=f"{DOC}{rule_id}",
        remediation=remediation,
    )


@rule("no-$ref-siblings", Severity.ERROR, "$ref siblings are ignored in OpenAPI 3.0.", ("3.0",))
def ref_siblings(document: dict):
    for node, path in _walk(document):
        if isinstance(node, dict) and "$ref" in node and len(node) > 1:
            siblings = sorted(k for k in node if k != "$ref")
            yield _finding(
                "no-$ref-siblings",
                Severity.ERROR,
                f"`$ref` has sibling keys {siblings}, which OpenAPI 3.0 ignores. "
                "The generated tool will use the referenced schema only.",
                "/".join(str(p) for p in path) or "(root)",
                pointer(*path),
                "Move the sibling keys into the referenced schema, or wrap the `$ref` in "
                "an `allOf`.",
            )


@rule("duplicated-entry-in-enum", Severity.WARNING, "Duplicate enum values are meaningless.")
def duplicated_enum(document: dict):
    for node, path in _walk(document):
        if not isinstance(node, dict):
            continue
        values = node.get("enum")
        if not isinstance(values, list):
            continue
        seen, duplicates = set(), []
        for value in values:
            key = repr(value)
            if key in seen:
                duplicates.append(value)
            seen.add(key)
        if duplicates:
            yield _finding(
                "duplicated-entry-in-enum",
                Severity.WARNING,
                f"Enum contains duplicate value(s) {duplicates}.",
                "/".join(str(p) for p in path) or "(root)",
                pointer(*path, "enum"),
                "Remove the duplicates.",
            )


@rule("typed-enum", Severity.WARNING, "Enum values must match the declared type.")
def typed_enum(document: dict):
    for node, path in _walk(document):
        if not isinstance(node, dict):
            continue
        declared = node.get("type")
        values = node.get("enum")
        if not isinstance(values, list) or not isinstance(declared, str):
            continue
        if declared not in _SCALARS:
            continue
        mismatched = [v for v in values if v is not None and not _matches(v, declared)]
        if mismatched:
            yield _finding(
                "typed-enum",
                Severity.WARNING,
                f"Enum value(s) {mismatched} do not match the declared type '{declared}'.",
                "/".join(str(p) for p in path) or "(root)",
                pointer(*path, "enum"),
                f"Make every enum value a {declared}, or correct the declared type.",
            )


def _matches(value, declared: str) -> bool:
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


@rule("sutr-parameter-description", Severity.INFO, "Parameter descriptions reach the agent.")
def parameter_description(document: dict):
    for ref in iter_operations(document):
        for param, ptr in effective_parameters(ref):
            if "$ref" in param:
                continue
            name = param.get("name")
            if not name:
                continue
            schema = param.get("schema") if isinstance(param.get("schema"), dict) else {}
            described = str(param.get("description") or schema.get("description") or "").strip()
            if not described:
                yield _finding(
                    "sutr-parameter-description",
                    Severity.INFO,
                    f"Parameter '{name}' has no description; it will appear in the tool's "
                    "input schema without guidance.",
                    ref.location,
                    f"{ptr}/description",
                    "Describe what the parameter means and what values are valid.",
                )


@rule("sutr-parameter-schema", Severity.WARNING, "A parameter without a schema is untyped.")
def parameter_schema(document: dict):
    for ref in iter_operations(document):
        for param, ptr in effective_parameters(ref):
            if "$ref" in param or not param.get("name"):
                continue
            if not isinstance(param.get("schema"), dict) and "content" not in param:
                yield _finding(
                    "sutr-parameter-schema",
                    Severity.WARNING,
                    f"Parameter '{param.get('name')}' declares no `schema`, so the generated "
                    "tool will treat it as a free-form string.",
                    ref.location,
                    f"{ptr}/schema",
                    "Add a `schema` with at least a `type`.",
                )


def _walk(node, path=()):
    """Yield every (node, path) pair in the document, depth-first."""
    yield node, path
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, path + (index,))
