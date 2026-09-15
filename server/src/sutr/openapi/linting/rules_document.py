"""Document-level rules: info block, servers, tags, unused components.

Rule ids match Spectral's `oas` ruleset where the same check exists, so a
finding is recognisable to anyone who already lints with Spectral. Rules with
no Spectral equivalent are prefixed `sutr-`.
"""

import re

from sutr.openapi.linting.finding import LintFinding, Severity, pointer
from sutr.openapi.linting.registry import rule
from sutr.openapi.linting.walk import iter_operations

DOC = "/openapi-linting#"

_SCRIPT_RE = re.compile(r"<script[\s>]", re.IGNORECASE)
_EVAL_RE = re.compile(r"\beval\s*\(")


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


@rule("oas3-api-servers", Severity.ERROR, "A server URL is required to call the API.")
def api_servers(document: dict):
    servers = document.get("servers")
    if not isinstance(servers, list) or not servers:
        yield _finding(
            "oas3-api-servers",
            Severity.ERROR,
            "The document declares no servers, so no base URL can be derived for generated tools.",
            "servers",
            pointer("servers"),
            "Add a top-level `servers` array with at least one entry, e.g. "
            "`servers: [{url: 'https://api.example.com/v1'}]`.",
        )
        return
    for index, server in enumerate(servers):
        if not isinstance(server, dict) or not server.get("url"):
            yield _finding(
                "oas3-api-servers",
                Severity.ERROR,
                f"Server entry {index} has no `url`.",
                f"servers[{index}]",
                pointer("servers", index),
                "Give every server entry a `url`.",
            )


@rule("info-description", Severity.WARNING, "The API should describe itself.")
def info_description(document: dict):
    info = document.get("info") or {}
    if not isinstance(info, dict) or not str(info.get("description") or "").strip():
        yield _finding(
            "info-description",
            Severity.WARNING,
            "`info.description` is missing. It is the best single source for the "
            "integration's description shown to agents.",
            "info",
            pointer("info", "description"),
            "Add `info.description` explaining what the API does.",
        )


@rule("info-contact", Severity.INFO, "Consumers should know who owns the API.")
def info_contact(document: dict):
    info = document.get("info") or {}
    if not isinstance(info, dict) or not isinstance(info.get("contact"), dict):
        yield _finding(
            "info-contact",
            Severity.INFO,
            "`info.contact` is missing.",
            "info",
            pointer("info", "contact"),
            "Add `info.contact` with a name, url, or email.",
        )


@rule("info-license", Severity.INFO, "The API's licence should be stated.")
def info_license(document: dict):
    info = document.get("info") or {}
    if not isinstance(info, dict) or not isinstance(info.get("license"), dict):
        yield _finding(
            "info-license",
            Severity.INFO,
            "`info.license` is missing.",
            "info",
            pointer("info", "license"),
            "Add `info.license` with a `name` and, ideally, a `url` or SPDX `identifier`.",
        )


@rule("openapi-tags", Severity.INFO, "Global tags let operations be grouped.")
def openapi_tags(document: dict):
    tags = document.get("tags")
    if not isinstance(tags, list) or not tags:
        yield _finding(
            "openapi-tags",
            Severity.INFO,
            "No global `tags` are declared, so operations cannot be filtered by tag on import.",
            "tags",
            pointer("tags"),
            "Declare a top-level `tags` array and reference the names from operations.",
        )


@rule("tag-description", Severity.INFO, "Declared tags should be described.")
def tag_description(document: dict):
    for index, tag in enumerate(document.get("tags") or []):
        if isinstance(tag, dict) and not str(tag.get("description") or "").strip():
            name = tag.get("name", index)
            yield _finding(
                "tag-description",
                Severity.INFO,
                f"Tag '{name}' has no description.",
                f"tags[{index}]",
                pointer("tags", index, "description"),
                "Describe what the tag groups.",
            )


@rule("oas3-unused-component", Severity.INFO, "Unreferenced components add noise.")
def unused_component(document: dict):
    components = document.get("components")
    if not isinstance(components, dict):
        return
    referenced = _collect_refs(document)
    for section, entries in components.items():
        if not isinstance(entries, dict):
            continue
        for name in entries:
            ref = f"#/components/{section}/{name}"
            if ref not in referenced:
                yield _finding(
                    "oas3-unused-component",
                    Severity.INFO,
                    f"Component `{section}.{name}` is never referenced.",
                    f"components.{section}.{name}",
                    pointer("components", section, name),
                    "Remove it, or reference it from an operation.",
                )


def _collect_refs(node, found=None) -> set:
    if found is None:
        found = set()
    if isinstance(node, dict):
        target = node.get("$ref")
        if isinstance(target, str):
            found.add(target)
        for key, value in node.items():
            if key != "$ref":
                _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)
    return found


@rule("path-keys-no-trailing-slash", Severity.WARNING, "Trailing slashes create duplicate routes.")
def path_trailing_slash(document: dict):
    for path in document.get("paths") or {}:
        if isinstance(path, str) and len(path) > 1 and path.endswith("/"):
            yield _finding(
                "path-keys-no-trailing-slash",
                Severity.WARNING,
                f"Path '{path}' ends with a slash; most servers treat it as a distinct route.",
                path,
                pointer("paths", path),
                "Remove the trailing slash.",
            )


@rule("path-declarations-must-exist", Severity.ERROR, "Empty path template segments are invalid.")
def path_declarations(document: dict):
    for path in document.get("paths") or {}:
        if isinstance(path, str) and "{}" in path:
            yield _finding(
                "path-declarations-must-exist",
                Severity.ERROR,
                f"Path '{path}' contains an empty template segment `{{}}`.",
                path,
                pointer("paths", path),
                "Name the path parameter, e.g. `{userId}`.",
            )


@rule("no-script-tags-in-markdown", Severity.WARNING, "Script tags in descriptions are unsafe.")
def no_script_tags(document: dict):
    for text, location, ptr in _iter_descriptions(document):
        if _SCRIPT_RE.search(text):
            yield _finding(
                "no-script-tags-in-markdown",
                Severity.WARNING,
                "A description contains a <script> tag; descriptions are rendered in the "
                "console and passed to agents.",
                location,
                ptr,
                "Remove the script tag from the description.",
            )


@rule("no-eval-in-markdown", Severity.WARNING, "eval() in descriptions is unsafe.")
def no_eval(document: dict):
    for text, location, ptr in _iter_descriptions(document):
        if _EVAL_RE.search(text):
            yield _finding(
                "no-eval-in-markdown",
                Severity.WARNING,
                "A description contains an `eval(` expression.",
                location,
                ptr,
                "Remove the eval expression from the description.",
            )


def _iter_descriptions(document: dict):
    info = document.get("info") or {}
    if isinstance(info, dict) and isinstance(info.get("description"), str):
        yield info["description"], "info", pointer("info", "description")
    for ref in iter_operations(document):
        for key in ("summary", "description"):
            value = ref.operation.get(key)
            if isinstance(value, str):
                yield value, ref.location, f"{ref.json_pointer}/{key}"
