"""What changed between two versions of an API, and whether it matters.

ESDS LLD §3.3 calls drift detection *"the governance differentiator"*: when a
provider attaches a second source, the platform continuously compares them and
reports the difference. Build prompt §13 fixes the vocabulary — every change is
classified BREAKING, NON_BREAKING, SECURITY, DOCUMENTATION or METADATA — and
adds the rule that matters: *"Breaking changes must not automatically deploy
unless policy permits it."*

The comparison runs over the **canonical IR**, never the raw documents. A
reformatted YAML file with reordered keys is not a change to the API, and a
diff that says otherwise is noise that trains people to ignore it.

The taxonomy follows `OpenAPITools/openapi-diff`, which is the reference for
what "breaking" means in practice, extended with the build prompt's SECURITY,
DOCUMENTATION and METADATA categories.

Breaking is judged from the **caller's** side. Removing an operation breaks
callers. Adding one does not. Making a parameter required breaks callers who
omitted it; making it optional does not.
"""

from dataclasses import dataclass, field
from enum import Enum


class Category(str, Enum):
    BREAKING = "BREAKING"
    NON_BREAKING = "NON_BREAKING"
    SECURITY = "SECURITY"
    DOCUMENTATION = "DOCUMENTATION"
    METADATA = "METADATA"


# Ordered by how much attention each deserves, so a report leads with the
# changes that can break somebody.
SEVERITY_ORDER = {
    Category.BREAKING: 0,
    Category.SECURITY: 1,
    Category.NON_BREAKING: 2,
    Category.METADATA: 3,
    Category.DOCUMENTATION: 4,
}


@dataclass(frozen=True)
class Change:
    """One difference between two IRs."""

    category: Category
    code: str
    summary: str
    # Where, in the API's own terms: "GET /pets", "components.securitySchemes.apiKey".
    location: str
    before: str | None = None
    after: str | None = None

    def as_dict(self) -> dict:
        return {
            "category": self.category.value,
            "code": self.code,
            "summary": self.summary,
            "location": self.location,
            "before": self.before,
            "after": self.after,
        }


@dataclass
class DiffReport:
    changes: list[Change] = field(default_factory=list)

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.category is Category.BREAKING]

    @property
    def security(self) -> list[Change]:
        return [c for c in self.changes if c.category is Category.SECURITY]

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def counts(self) -> dict[str, int]:
        counts = {category.value: 0 for category in Category}
        for change in self.changes:
            counts[change.category.value] += 1
        return counts

    def as_dict(self) -> dict:
        return {
            "changes": [change.as_dict() for change in self.changes],
            "counts": self.counts(),
            "breaking": len(self.breaking),
            "has_changes": self.has_changes,
        }


def _operation_key(operation) -> str:
    return f"{operation.method} {operation.path}"


def _param_key(param) -> str:
    return f"{param.name} ({param.location})"


def _schema_type(schema: dict | None) -> str | None:
    if not isinstance(schema, dict):
        return None
    value = schema.get("type")
    return value if isinstance(value, str) else None


def compare(before, after) -> DiffReport:
    """Every difference between two `ApiDefinition`s, classified.

    `before` is what the platform holds; `after` is what the source now says.
    """
    changes: list[Change] = []
    changes += _compare_info(before, after)
    changes += _compare_servers(before, after)
    changes += _compare_security(before, after)
    changes += _compare_operations(before, after)
    changes.sort(key=lambda change: (SEVERITY_ORDER[change.category], change.location, change.code))
    return DiffReport(changes=changes)


# ── info / metadata ──────────────────────────────────────────────────────────


def _compare_info(before, after) -> list[Change]:
    changes: list[Change] = []
    if before.version != after.version:
        changes.append(
            Change(
                category=Category.METADATA,
                code="api_version_changed",
                summary=f"API version {before.version} → {after.version}.",
                location="info.version",
                before=before.version,
                after=after.version,
            )
        )
    if before.title != after.title:
        changes.append(
            Change(
                category=Category.METADATA,
                code="api_title_changed",
                summary=f"API title changed to '{after.title}'.",
                location="info.title",
                before=before.title,
                after=after.title,
            )
        )
    if (before.description or "") != (after.description or ""):
        changes.append(
            Change(
                category=Category.DOCUMENTATION,
                code="api_description_changed",
                summary="The API description changed.",
                location="info.description",
            )
        )
    if before.openapi_version != after.openapi_version:
        changes.append(
            Change(
                category=Category.METADATA,
                code="openapi_version_changed",
                summary=(f"OpenAPI version {before.openapi_version} → {after.openapi_version}."),
                location="openapi",
                before=before.openapi_version,
                after=after.openapi_version,
            )
        )
    return changes


def _compare_servers(before, after) -> list[Change]:
    old = {server.url for server in before.servers}
    new = {server.url for server in after.servers}
    changes: list[Change] = []
    for removed in sorted(old - new):
        # A server URL that disappears is breaking: anything pinned to it now
        # points at nothing.
        changes.append(
            Change(
                category=Category.BREAKING,
                code="server_removed",
                summary=f"Server {removed} was removed.",
                location="servers",
                before=removed,
            )
        )
    for added in sorted(new - old):
        changes.append(
            Change(
                category=Category.NON_BREAKING,
                code="server_added",
                summary=f"Server {added} was added.",
                location="servers",
                after=added,
            )
        )
    return changes


# ── security ─────────────────────────────────────────────────────────────────


def _compare_security(before, after) -> list[Change]:
    """Every security difference is SECURITY, whichever direction it moves.

    Adding authentication is not "non-breaking because it is safer" — existing
    callers stop working. Removing it is not "non-breaking because callers keep
    working" — the API just became public. Both need a human to look.
    """
    changes: list[Change] = []
    old = {scheme.name: scheme for scheme in before.security_schemes}
    new = {scheme.name: scheme for scheme in after.security_schemes}

    for name in sorted(set(old) - set(new)):
        changes.append(
            Change(
                category=Category.SECURITY,
                code="security_scheme_removed",
                summary=f"Security scheme '{name}' was removed.",
                location=f"components.securitySchemes.{name}",
                before=old[name].type,
            )
        )
    for name in sorted(set(new) - set(old)):
        changes.append(
            Change(
                category=Category.SECURITY,
                code="security_scheme_added",
                summary=f"Security scheme '{name}' ({new[name].type}) was added.",
                location=f"components.securitySchemes.{name}",
                after=new[name].type,
            )
        )
    for name in sorted(set(old) & set(new)):
        old_scheme, new_scheme = old[name], new[name]
        for attribute in ("type", "scheme", "location", "param_name"):
            old_value = getattr(old_scheme, attribute, None)
            new_value = getattr(new_scheme, attribute, None)
            if old_value != new_value:
                changes.append(
                    Change(
                        category=Category.SECURITY,
                        code="security_scheme_changed",
                        summary=(
                            f"Security scheme '{name}' changed its {attribute}: "
                            f"{old_value} → {new_value}."
                        ),
                        location=f"components.securitySchemes.{name}",
                        before=str(old_value),
                        after=str(new_value),
                    )
                )

    old_global = set(before.global_security)
    new_global = set(after.global_security)
    if old_global != new_global:
        changes.append(
            Change(
                category=Category.SECURITY,
                code="global_security_changed",
                summary=(
                    f"The API's default security requirement changed: "
                    f"{sorted(old_global) or 'none'} → {sorted(new_global) or 'none'}."
                ),
                location="security",
                before=", ".join(sorted(old_global)) or None,
                after=", ".join(sorted(new_global)) or None,
            )
        )
    return changes


# ── operations ───────────────────────────────────────────────────────────────


def _compare_operations(before, after) -> list[Change]:  # noqa: C901 — one rule per branch
    changes: list[Change] = []
    old = {_operation_key(op): op for op in before.operations}
    new = {_operation_key(op): op for op in after.operations}

    for key in sorted(set(old) - set(new)):
        changes.append(
            Change(
                category=Category.BREAKING,
                code="operation_removed",
                summary=f"{key} was removed.",
                location=key,
                before=old[key].operation_id,
            )
        )
    for key in sorted(set(new) - set(old)):
        changes.append(
            Change(
                category=Category.NON_BREAKING,
                code="operation_added",
                summary=f"{key} was added.",
                location=key,
                after=new[key].operation_id,
            )
        )

    for key in sorted(set(old) & set(new)):
        changes += _compare_one_operation(key, old[key], new[key])
    return changes


def _compare_one_operation(key: str, old, new) -> list[Change]:  # noqa: C901
    changes: list[Change] = []

    if old.operation_id != new.operation_id:
        # The operationId becomes the tool name, so changing it renames a tool
        # that agents may already be calling.
        changes.append(
            Change(
                category=Category.BREAKING,
                code="operation_id_changed",
                summary=(
                    f"{key} changed its operationId ({old.operation_id} → "
                    f"{new.operation_id}), which renames the generated tool."
                ),
                location=key,
                before=old.operation_id,
                after=new.operation_id,
            )
        )

    if new.deprecated and not old.deprecated:
        changes.append(
            Change(
                category=Category.NON_BREAKING,
                code="operation_deprecated",
                summary=f"{key} was marked deprecated.",
                location=key,
            )
        )
    elif old.deprecated and not new.deprecated:
        changes.append(
            Change(
                category=Category.NON_BREAKING,
                code="operation_undeprecated",
                summary=f"{key} is no longer deprecated.",
                location=key,
            )
        )

    if (old.summary or "") != (new.summary or "") or (old.description or "") != (
        new.description or ""
    ):
        changes.append(
            Change(
                category=Category.DOCUMENTATION,
                code="operation_documentation_changed",
                summary=f"{key} changed its summary or description.",
                location=key,
            )
        )

    if set(old.tags) != set(new.tags):
        changes.append(
            Change(
                category=Category.METADATA,
                code="operation_tags_changed",
                summary=f"{key} changed its tags.",
                location=key,
                before=", ".join(sorted(old.tags)) or None,
                after=", ".join(sorted(new.tags)) or None,
            )
        )

    changes += _compare_parameters(key, old, new)
    changes += _compare_body(key, old, new)
    changes += _compare_responses(key, old, new)

    old_security = set(old.security_schemes or [])
    new_security = set(new.security_schemes or [])
    if (old.security_schemes is None) != (new.security_schemes is None) or (
        old_security != new_security
    ):
        changes.append(
            Change(
                category=Category.SECURITY,
                code="operation_security_changed",
                summary=f"{key} changed its security requirement.",
                location=key,
                before=", ".join(sorted(old_security)) or None,
                after=", ".join(sorted(new_security)) or None,
            )
        )
    return changes


def _compare_parameters(key: str, old, new) -> list[Change]:
    changes: list[Change] = []
    old_params = {_param_key(param): param for param in old.parameters}
    new_params = {_param_key(param): param for param in new.parameters}

    for name in sorted(set(old_params) - set(new_params)):
        param = old_params[name]
        # Removing a parameter a caller is sending: the value is now ignored,
        # which changes behaviour silently rather than loudly.
        changes.append(
            Change(
                category=Category.BREAKING if param.required else Category.NON_BREAKING,
                code="parameter_removed",
                summary=f"{key} no longer accepts parameter {name}.",
                location=key,
                before=name,
            )
        )
    for name in sorted(set(new_params) - set(old_params)):
        param = new_params[name]
        # A newly required parameter breaks every existing caller.
        changes.append(
            Change(
                category=Category.BREAKING if param.required else Category.NON_BREAKING,
                code="parameter_added",
                summary=(
                    f"{key} added {'required' if param.required else 'optional'} parameter {name}."
                ),
                location=key,
                after=name,
            )
        )
    for name in sorted(set(old_params) & set(new_params)):
        old_param, new_param = old_params[name], new_params[name]
        if new_param.required and not old_param.required:
            changes.append(
                Change(
                    category=Category.BREAKING,
                    code="parameter_became_required",
                    summary=f"{key} now requires parameter {name}.",
                    location=key,
                )
            )
        elif old_param.required and not new_param.required:
            changes.append(
                Change(
                    category=Category.NON_BREAKING,
                    code="parameter_became_optional",
                    summary=f"{key} no longer requires parameter {name}.",
                    location=key,
                )
            )
        old_type = _schema_type(old_param.json_schema)
        new_type = _schema_type(new_param.json_schema)
        if old_type != new_type:
            changes.append(
                Change(
                    category=Category.BREAKING,
                    code="parameter_type_changed",
                    summary=f"{key} changed the type of {name}: {old_type} → {new_type}.",
                    location=key,
                    before=old_type,
                    after=new_type,
                )
            )
        if (old_param.description or "") != (new_param.description or ""):
            changes.append(
                Change(
                    category=Category.DOCUMENTATION,
                    code="parameter_documentation_changed",
                    summary=f"{key} changed the description of {name}.",
                    location=key,
                )
            )
    return changes


def _compare_body(key: str, old, new) -> list[Change]:
    changes: list[Change] = []
    old_body, new_body = old.request_body, new.request_body

    if old_body is None and new_body is not None:
        changes.append(
            Change(
                category=Category.BREAKING if new_body.required else Category.NON_BREAKING,
                code="request_body_added",
                summary=(
                    f"{key} added a {'required' if new_body.required else 'optional'} "
                    f"request body ({new_body.content_type})."
                ),
                location=key,
                after=new_body.content_type,
            )
        )
    elif old_body is not None and new_body is None:
        changes.append(
            Change(
                category=Category.BREAKING,
                code="request_body_removed",
                summary=f"{key} no longer accepts a request body.",
                location=key,
                before=old_body.content_type,
            )
        )
    elif old_body is not None and new_body is not None:
        if old_body.content_type != new_body.content_type:
            changes.append(
                Change(
                    category=Category.BREAKING,
                    code="request_body_media_type_changed",
                    summary=(
                        f"{key} changed its request body media type: "
                        f"{old_body.content_type} → {new_body.content_type}."
                    ),
                    location=key,
                    before=old_body.content_type,
                    after=new_body.content_type,
                )
            )
        if new_body.required and not old_body.required:
            changes.append(
                Change(
                    category=Category.BREAKING,
                    code="request_body_became_required",
                    summary=f"{key} now requires a request body.",
                    location=key,
                )
            )
        changes += _compare_body_properties(key, old_body, new_body)
    return changes


def _compare_body_properties(key: str, old_body, new_body) -> list[Change]:
    """Top-level body properties, which is what becomes tool arguments."""
    changes: list[Change] = []
    old_props = (old_body.json_schema or {}).get("properties") or {}
    new_props = (new_body.json_schema or {}).get("properties") or {}
    if not isinstance(old_props, dict) or not isinstance(new_props, dict):
        return changes
    old_required = set((old_body.json_schema or {}).get("required") or [])
    new_required = set((new_body.json_schema or {}).get("required") or [])

    for name in sorted(set(old_props) - set(new_props)):
        changes.append(
            Change(
                category=Category.BREAKING if name in old_required else Category.NON_BREAKING,
                code="body_property_removed",
                summary=f"{key} removed body property '{name}'.",
                location=key,
                before=name,
            )
        )
    for name in sorted(set(new_props) - set(old_props)):
        changes.append(
            Change(
                category=Category.BREAKING if name in new_required else Category.NON_BREAKING,
                code="body_property_added",
                summary=(
                    f"{key} added {'required' if name in new_required else 'optional'} "
                    f"body property '{name}'."
                ),
                location=key,
                after=name,
            )
        )
    for name in sorted((new_required - old_required) & set(old_props) & set(new_props)):
        changes.append(
            Change(
                category=Category.BREAKING,
                code="body_property_became_required",
                summary=f"{key} now requires body property '{name}'.",
                location=key,
            )
        )
    return changes


def _compare_responses(key: str, old, new) -> list[Change]:
    changes: list[Change] = []
    old_codes = set(old.responses)
    new_codes = set(new.responses)
    for removed in sorted(old_codes - new_codes):
        # A documented response disappearing is a documentation change, not a
        # contract break: the server may still return it. Callers that branch
        # on it are not broken by the document.
        changes.append(
            Change(
                category=Category.DOCUMENTATION,
                code="response_removed",
                summary=f"{key} no longer documents a {removed} response.",
                location=key,
                before=removed,
            )
        )
    for added in sorted(new_codes - old_codes):
        changes.append(
            Change(
                category=Category.DOCUMENTATION,
                code="response_added",
                summary=f"{key} documents a new {added} response.",
                location=key,
                after=added,
            )
        )
    return changes
