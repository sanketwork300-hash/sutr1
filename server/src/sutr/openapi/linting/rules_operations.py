"""Operation-level rules.

These matter more here than in a generic linter: an operation's `operationId`
becomes the MCP tool name, its summary and description become the text an agent
reasons over, and its parameters become the tool's input schema. A finding here
is a prediction about how good the generated tool will be.
"""

import re

from sutr.openapi.linting.finding import LintFinding, Severity
from sutr.openapi.linting.registry import rule
from sutr.openapi.linting.walk import effective_parameters, iter_operations

DOC = "/openapi-linting#"

_PATH_TEMPLATE_RE = re.compile(r"\{([^{}]*)\}")
_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9._~\-]+$")


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


@rule("operation-operationId", Severity.WARNING, "operationId becomes the tool name.")
def operation_id_present(document: dict):
    for ref in iter_operations(document):
        if not str(ref.operation.get("operationId") or "").strip():
            yield _finding(
                "operation-operationId",
                Severity.WARNING,
                "No `operationId`. The generated tool name will be derived from the method "
                "and path instead, which reads worse to an agent.",
                ref.location,
                f"{ref.json_pointer}/operationId",
                "Add a descriptive `operationId`, e.g. `listCustomerPayments`.",
            )


@rule("operation-operationId-unique", Severity.ERROR, "Duplicate operationIds collide.")
def operation_id_unique(document: dict):
    seen: dict[str, str] = {}
    for ref in iter_operations(document):
        operation_id = str(ref.operation.get("operationId") or "").strip()
        if not operation_id:
            continue
        if operation_id in seen:
            yield _finding(
                "operation-operationId-unique",
                Severity.ERROR,
                f"`operationId` '{operation_id}' is already used by {seen[operation_id]}.",
                ref.location,
                f"{ref.json_pointer}/operationId",
                "Make every operationId unique; the compiler must otherwise rename tools.",
            )
        else:
            seen[operation_id] = ref.location


@rule("operation-operationId-valid-in-url", Severity.WARNING, "operationIds should be URL-safe.")
def operation_id_url_safe(document: dict):
    for ref in iter_operations(document):
        operation_id = str(ref.operation.get("operationId") or "").strip()
        if operation_id and not _URL_SAFE_RE.match(operation_id):
            yield _finding(
                "operation-operationId-valid-in-url",
                Severity.WARNING,
                f"`operationId` '{operation_id}' contains characters that are not URL-safe.",
                ref.location,
                f"{ref.json_pointer}/operationId",
                "Restrict operationIds to letters, digits, `.`, `_`, `~` and `-`.",
            )


@rule("operation-description", Severity.WARNING, "Descriptions drive agent tool selection.")
def operation_description(document: dict):
    for ref in iter_operations(document):
        has_summary = bool(str(ref.operation.get("summary") or "").strip())
        has_description = bool(str(ref.operation.get("description") or "").strip())
        if not has_summary and not has_description:
            yield _finding(
                "operation-description",
                Severity.WARNING,
                "The operation has neither a summary nor a description, so the generated "
                "tool description can only state its HTTP method and path.",
                ref.location,
                f"{ref.json_pointer}/description",
                "Add a one-line `summary` and, where useful, a longer `description`.",
            )


@rule("operation-tags", Severity.INFO, "Tags make selective import possible.")
def operation_tags(document: dict):
    for ref in iter_operations(document):
        tags = ref.operation.get("tags")
        if not isinstance(tags, list) or not tags:
            yield _finding(
                "operation-tags",
                Severity.INFO,
                "The operation has no tags, so it cannot be selected by tag on import.",
                ref.location,
                f"{ref.json_pointer}/tags",
                "Tag the operation with the resource or capability it belongs to.",
            )


@rule("operation-tag-defined", Severity.WARNING, "Operation tags should be declared globally.")
def operation_tag_defined(document: dict):
    declared = {
        tag.get("name")
        for tag in (document.get("tags") or [])
        if isinstance(tag, dict) and tag.get("name")
    }
    if not declared:
        return  # openapi-tags already reports the absence
    for ref in iter_operations(document):
        for tag in ref.operation.get("tags") or []:
            if isinstance(tag, str) and tag not in declared:
                yield _finding(
                    "operation-tag-defined",
                    Severity.WARNING,
                    f"Tag '{tag}' is used but not declared in the global `tags` array.",
                    ref.location,
                    f"{ref.json_pointer}/tags",
                    f"Add '{tag}' to the top-level `tags` array, or correct the spelling.",
                )


@rule("operation-success-response", Severity.WARNING, "An operation must describe success.")
def operation_success_response(document: dict):
    for ref in iter_operations(document):
        responses = ref.operation.get("responses")
        if not isinstance(responses, dict):
            responses = {}
        codes = [str(code) for code in responses]
        if not any(code.startswith(("2", "3")) or code == "default" for code in codes):
            yield _finding(
                "operation-success-response",
                Severity.WARNING,
                "The operation declares no 2xx/3xx response, so the generated tool cannot "
                "tell an agent what a successful call returns.",
                ref.location,
                f"{ref.json_pointer}/responses",
                "Declare at least one success response with a description.",
            )


@rule("operation-parameters", Severity.ERROR, "Parameters must be unique by (name, in).")
def operation_parameters_unique(document: dict):
    for ref in iter_operations(document):
        seen: set[tuple[str, str]] = set()
        for param, ptr in effective_parameters(ref):
            key = (str(param.get("name")), str(param.get("in")))
            if key[0] == "None" or key[1] == "None":
                continue
            if key in seen:
                yield _finding(
                    "operation-parameters",
                    Severity.ERROR,
                    f"Parameter '{key[0]}' in {key[1]} is declared more than once.",
                    ref.location,
                    ptr,
                    "Remove the duplicate; a path-item parameter is already inherited by "
                    "every operation on that path.",
                )
            seen.add(key)


@rule("path-params", Severity.ERROR, "Path template and declared path parameters must agree.")
def path_params(document: dict):
    for ref in iter_operations(document):
        templated = {name for name in _PATH_TEMPLATE_RE.findall(ref.path) if name and name.strip()}
        declared = {
            str(param.get("name"))
            for param, _ in effective_parameters(ref)
            if param.get("in") == "path" and param.get("name")
        }
        for missing in sorted(templated - declared):
            yield _finding(
                "path-params",
                Severity.ERROR,
                f"Path parameter '{{{missing}}}' appears in the path but is not declared, "
                "so the generated tool would send the placeholder literally.",
                ref.location,
                f"{ref.json_pointer}/parameters",
                f"Declare a parameter with `name: {missing}` and `in: path`, `required: true`.",
            )
        for extra in sorted(declared - templated):
            yield _finding(
                "path-params",
                Severity.ERROR,
                f"Path parameter '{extra}' is declared but does not appear in the path.",
                ref.location,
                f"{ref.json_pointer}/parameters",
                f"Remove the parameter, or add `{{{extra}}}` to the path.",
            )


@rule("sutr-path-param-required", Severity.ERROR, "Path parameters are always required.")
def path_param_required(document: dict):
    for ref in iter_operations(document):
        for param, ptr in effective_parameters(ref):
            if param.get("in") == "path" and param.get("required") is False:
                yield _finding(
                    "sutr-path-param-required",
                    Severity.ERROR,
                    f"Path parameter '{param.get('name')}' is marked `required: false`, "
                    "which the OpenAPI specification does not permit.",
                    ref.location,
                    f"{ptr}/required",
                    "Set `required: true` on every path parameter.",
                )
