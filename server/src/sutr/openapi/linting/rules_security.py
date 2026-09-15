"""Security rules.

An API with no declared security is either public or under-documented, and the
difference matters: the compiler cannot inject a credential it was never told
about, so the generated tool will simply get 401s at runtime.
"""

from sutr.openapi.linting.finding import LintFinding, Severity, pointer
from sutr.openapi.linting.registry import rule
from sutr.openapi.linting.walk import iter_operations

DOC = "/openapi-linting#"

_KNOWN_TYPES = ("apiKey", "http", "oauth2", "openIdConnect", "mutualTLS")


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


def _schemes(document: dict) -> dict:
    components = document.get("components")
    if not isinstance(components, dict):
        return {}
    schemes = components.get("securitySchemes")
    return schemes if isinstance(schemes, dict) else {}


@rule("oas3-operation-security-defined", Severity.ERROR, "Security requirements must resolve.")
def security_defined(document: dict):
    declared = set(_schemes(document))
    for requirement, location, ptr in _iter_requirements(document):
        for name in requirement:
            if name not in declared:
                yield _finding(
                    "oas3-operation-security-defined",
                    Severity.ERROR,
                    f"Security requirement '{name}' is not defined in "
                    "`components.securitySchemes`.",
                    location,
                    ptr,
                    f"Define '{name}' under `components.securitySchemes`, or remove the "
                    "requirement.",
                )


def _iter_requirements(document: dict):
    for index, requirement in enumerate(document.get("security") or []):
        if isinstance(requirement, dict):
            yield requirement, "security", pointer("security", index)
    for ref in iter_operations(document):
        for index, requirement in enumerate(ref.operation.get("security") or []):
            if isinstance(requirement, dict):
                yield requirement, ref.location, f"{ref.json_pointer}/security/{index}"


@rule("sutr-security-declared", Severity.WARNING, "An API with no security is treated as public.")
def security_declared(document: dict):
    if document.get("security") or _schemes(document):
        return
    has_operation_security = any(ref.operation.get("security") for ref in iter_operations(document))
    if has_operation_security:
        return
    yield _finding(
        "sutr-security-declared",
        Severity.WARNING,
        "The document declares no security schemes. Generated tools will send no "
        "credential, which is correct only if the API is genuinely public.",
        "(root)",
        pointer("components", "securitySchemes"),
        "Declare the API's authentication under `components.securitySchemes` and "
        "reference it from `security`.",
    )


@rule("sutr-security-scheme-type", Severity.ERROR, "Security scheme types must be valid.")
def scheme_type_valid(document: dict):
    for name, scheme in _schemes(document).items():
        if not isinstance(scheme, dict):
            continue
        scheme_type = scheme.get("type")
        if scheme_type not in _KNOWN_TYPES:
            yield _finding(
                "sutr-security-scheme-type",
                Severity.ERROR,
                f"Security scheme '{name}' has type '{scheme_type}', which is not one of "
                f"{list(_KNOWN_TYPES)}.",
                f"components.securitySchemes.{name}",
                pointer("components", "securitySchemes", name, "type"),
                "Use one of the types defined by the OpenAPI specification.",
            )


@rule("sutr-api-key-location", Severity.ERROR, "apiKey schemes need a name and a location.")
def api_key_complete(document: dict):
    for name, scheme in _schemes(document).items():
        if not isinstance(scheme, dict) or scheme.get("type") != "apiKey":
            continue
        if not scheme.get("name") or scheme.get("in") not in ("header", "query", "cookie"):
            yield _finding(
                "sutr-api-key-location",
                Severity.ERROR,
                f"apiKey scheme '{name}' must declare both `name` and `in` "
                "(header, query, or cookie).",
                f"components.securitySchemes.{name}",
                pointer("components", "securitySchemes", name),
                "Add `name: X-Api-Key` and `in: header` (or the API's actual placement).",
            )


@rule("sutr-oauth2-flows", Severity.ERROR, "OAuth2 schemes must declare their flows.")
def oauth2_flows(document: dict):
    for name, scheme in _schemes(document).items():
        if not isinstance(scheme, dict) or scheme.get("type") != "oauth2":
            continue
        flows = scheme.get("flows")
        if not isinstance(flows, dict) or not flows:
            yield _finding(
                "sutr-oauth2-flows",
                Severity.ERROR,
                f"OAuth2 scheme '{name}' declares no `flows`, so no grant can be run for it.",
                f"components.securitySchemes.{name}",
                pointer("components", "securitySchemes", name, "flows"),
                "Declare at least one flow (`clientCredentials` or `authorizationCode`) "
                "with its token URL.",
            )
            continue
        for flow_name, flow in flows.items():
            if not isinstance(flow, dict):
                continue
            if flow_name in ("clientCredentials", "password") and not flow.get("tokenUrl"):
                yield _finding(
                    "sutr-oauth2-flows",
                    Severity.ERROR,
                    f"OAuth2 flow '{flow_name}' on scheme '{name}' has no `tokenUrl`.",
                    f"components.securitySchemes.{name}.flows.{flow_name}",
                    pointer("components", "securitySchemes", name, "flows", flow_name, "tokenUrl"),
                    "Add the provider's token endpoint URL.",
                )
            if flow_name == "authorizationCode" and not (
                flow.get("tokenUrl") and flow.get("authorizationUrl")
            ):
                yield _finding(
                    "sutr-oauth2-flows",
                    Severity.ERROR,
                    f"OAuth2 authorizationCode flow on scheme '{name}' needs both "
                    "`authorizationUrl` and `tokenUrl`.",
                    f"components.securitySchemes.{name}.flows.authorizationCode",
                    pointer("components", "securitySchemes", name, "flows", flow_name),
                    "Add both endpoint URLs.",
                )
