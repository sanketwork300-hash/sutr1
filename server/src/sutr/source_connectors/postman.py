"""Postman collections, converted to OpenAPI.

The LLD (§3.3) lists Postman collections among the places definitions live, and
it is often the *only* place: plenty of teams have a collection and no
specification at all.

A collection is not a specification, and the conversion is lossy in one
direction that matters: a collection records **examples of requests**, not the
contract behind them. There is no schema for a parameter, only a value someone
once sent. So this converter infers types from example values and says so — the
generated tool describes what was observed, not what is allowed.

Supports Postman Collection Format **v2.0 and v2.1**. v1 is a different shape
and is refused with a message naming the exporter setting that fixes it, rather
than half-parsed.
"""

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sutr.source_connectors.base import (
    WATCH_NONE,
    ConfigField,
    ConnectionResult,
    ConnectorError,
    FetchResult,
    Provenance,
    SourceConnector,
    WatchPlan,
)

SUPPORTED_SCHEMAS = ("v2.0.0", "v2.1.0")

_TYPE_BY_PYTHON = {bool: "boolean", int: "integer", float: "number", str: "string"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_postman_collection(document: dict) -> bool:
    info = document.get("info")
    return (
        isinstance(info, dict)
        and ("_postman_id" in info or "schema" in info)
        and ("item" in document)
    )


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return _TYPE_BY_PYTHON[type(value)]
    if isinstance(value, str):
        # Postman values are always strings; guess only where it is unambiguous.
        lowered = value.strip().lower()
        if lowered in ("true", "false"):
            return "boolean"
        try:
            int(value)
            return "integer"
        except (TypeError, ValueError):
            pass
        try:
            float(value)
            return "number"
        except (TypeError, ValueError):
            pass
    return "string"


def _resolve_variables(text: str, variables: dict[str, str]) -> str:
    """Substitute `{{var}}` with a collection variable's value."""
    for name, value in variables.items():
        text = text.replace("{{" + name + "}}", str(value))
    return text


def _collection_variables(collection: dict) -> dict[str, str]:
    values = {}
    for entry in collection.get("variable") or []:
        if isinstance(entry, dict) and entry.get("key"):
            values[str(entry["key"])] = str(entry.get("value", ""))
    return values


def _raw_url(request: dict, variables: dict[str, str]) -> str:
    url = request.get("url")
    if isinstance(url, str):
        return _resolve_variables(url, variables)
    if isinstance(url, dict):
        raw = url.get("raw")
        if isinstance(raw, str):
            return _resolve_variables(raw, variables)
        host = url.get("host")
        host_text = ".".join(host) if isinstance(host, list) else str(host or "")
        path = url.get("path")
        path_text = (
            "/".join(str(part) for part in path) if isinstance(path, list) else str(path or "")
        )
        protocol = url.get("protocol") or "https"
        return _resolve_variables(f"{protocol}://{host_text}/{path_text}", variables)
    return ""


def _split_url(raw: str) -> tuple[str, str]:
    """(server, path) from a full URL, with Postman's `:param` made OpenAPI's `{param}`."""
    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return "", raw or "/"
    server = f"{parts.scheme}://{parts.netloc}"
    path = parts.path or "/"
    segments = []
    for segment in path.split("/"):
        if segment.startswith(":") and len(segment) > 1:
            segments.append("{" + segment[1:] + "}")
        else:
            segments.append(segment)
    return server, "/".join(segments) or "/"


def _walk_items(items: list, folder: str = "") -> list[tuple[str, dict]]:
    """Flatten Postman's nested folders into (folder path, request item)."""
    found: list[tuple[str, dict]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if isinstance(item.get("item"), list):
            child_folder = f"{folder}/{name}".strip("/")
            found += _walk_items(item["item"], child_folder)
        elif isinstance(item.get("request"), (dict, str)):
            found.append((folder, item))
    return found


def _operation_id(name: str, method: str, path: str, used: set[str]) -> str:
    import re

    base = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_") or f"{method.lower()}_{path.strip('/')}"
    base = base[:60] or "operation"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def convert_collection(collection: dict) -> tuple[dict, list[str]]:
    """Postman collection → an OpenAPI 3.0 document, plus conversion notes.

    The notes are returned rather than logged: a lossy conversion should tell
    the user what it inferred, so they can correct the parts that matter.
    """
    info = collection.get("info") or {}
    schema = str(info.get("schema") or "")
    if schema and not any(version in schema for version in SUPPORTED_SCHEMAS):
        raise ConnectorError(
            "unsupported_collection",
            "Only Postman Collection v2.0 and v2.1 are supported. Re-export the collection "
            "as v2.1 (Postman → Export → Collection v2.1).",
        )

    notes: list[str] = [
        "Converted from a Postman collection. A collection records example requests rather "
        "than a contract, so parameter types are inferred from the values in the examples."
    ]
    variables = _collection_variables(collection)
    servers: dict[str, int] = {}
    paths: dict[str, dict] = {}
    used_ids: set[str] = set()

    for folder, item in _walk_items(collection.get("item") or []):
        request = item.get("request")
        if isinstance(request, str):
            request = {"method": "GET", "url": request}
        if not isinstance(request, dict):
            continue

        method = str(request.get("method") or "GET").lower()
        raw = _raw_url(request, variables)
        if not raw:
            notes.append(f"Skipped '{item.get('name')}': it has no URL.")
            continue
        server, path = _split_url(raw)
        if server:
            servers[server] = servers.get(server, 0) + 1

        operation: dict[str, Any] = {
            "operationId": _operation_id(str(item.get("name") or ""), method, path, used_ids),
            "summary": str(item.get("name") or f"{method.upper()} {path}")[:200],
            "responses": {"200": {"description": "Successful response."}},
        }
        if folder:
            operation["tags"] = [folder.split("/")[0]]
        description = item.get("description") or request.get("description")
        if isinstance(description, str) and description.strip():
            operation["description"] = description.strip()[:2000]
        elif isinstance(description, dict) and description.get("content"):
            operation["description"] = str(description["content"])[:2000]

        parameters = _parameters_from(request, raw, path, variables)
        if parameters:
            operation["parameters"] = parameters

        body = _body_from(request, notes, item.get("name"))
        if body is not None:
            operation["requestBody"] = body

        paths.setdefault(path, {})[method] = operation

    if not paths:
        raise ConnectorError(
            "no_requests", "The collection contains no requests that could be converted."
        )

    document: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": str(info.get("name") or "Imported collection"),
            "version": "1.0.0",
        },
        "paths": paths,
    }
    collection_description = info.get("description")
    if isinstance(collection_description, str) and collection_description.strip():
        document["info"]["description"] = collection_description.strip()[:4000]
    elif isinstance(collection_description, dict) and collection_description.get("content"):
        document["info"]["description"] = str(collection_description["content"])[:4000]

    if servers:
        ordered = sorted(servers.items(), key=lambda kv: (-kv[1], kv[0]))
        document["servers"] = [{"url": url} for url, _ in ordered]
        if len(ordered) > 1:
            notes.append(
                f"The collection uses {len(ordered)} different hosts; "
                f"'{ordered[0][0]}' is listed first."
            )
    else:
        notes.append("No absolute URL was found, so no server could be derived.")

    security = _security_from(collection)
    if security:
        document["components"] = {"securitySchemes": security["schemes"]}
        document["security"] = security["requirement"]
        notes.append(security["note"])

    return document, notes


def _parameters_from(request: dict, raw: str, path: str, variables: dict[str, str]) -> list[dict]:
    parameters: list[dict] = []
    url = request.get("url")

    for segment in path.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            parameters.append(
                {
                    "name": segment[1:-1],
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            )

    if isinstance(url, dict):
        for query in url.get("query") or []:
            if not isinstance(query, dict) or not query.get("key"):
                continue
            if query.get("disabled"):
                continue
            value = query.get("value")
            parameters.append(
                {
                    "name": str(query["key"]),
                    "in": "query",
                    "required": False,
                    "description": str(query.get("description") or "") or None,
                    "schema": {"type": _infer_type(value)},
                }
            )

    for header in request.get("header") or []:
        if not isinstance(header, dict) or not header.get("key"):
            continue
        if header.get("disabled"):
            continue
        name = str(header["key"])
        # Authorization is a security scheme, not a parameter; emitting it as
        # one would put a credential in the tool's argument list.
        if name.lower() in ("authorization", "content-type", "accept"):
            continue
        parameters.append(
            {
                "name": name,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
            }
        )

    return [
        {key: value for key, value in parameter.items() if value is not None}
        for parameter in parameters
    ]


def _body_from(request: dict, notes: list[str], item_name) -> dict | None:
    body = request.get("body")
    if not isinstance(body, dict):
        return None
    mode = body.get("mode")

    if mode == "raw":
        raw = body.get("raw")
        if not isinstance(raw, str) or not raw.strip():
            return None
        language = ((body.get("options") or {}).get("raw") or {}).get("language")
        if language == "json" or raw.lstrip().startswith(("{", "[")):
            try:
                example = json.loads(raw)
            except ValueError:
                notes.append(
                    f"'{item_name}' has a raw JSON body that does not parse; it was carried "
                    "over as a free-form object."
                )
                return {"content": {"application/json": {"schema": {"type": "object"}}}}
            return {"content": {"application/json": {"schema": _schema_from_example(example)}}}
        return {"content": {"text/plain": {"schema": {"type": "string"}}}}

    if mode in ("urlencoded", "formdata"):
        fields = body.get(mode) or []
        properties = {}
        required = []
        has_file = False
        for entry in fields:
            if not isinstance(entry, dict) or not entry.get("key") or entry.get("disabled"):
                continue
            name = str(entry["key"])
            if entry.get("type") == "file":
                properties[name] = {"type": "string", "format": "binary"}
                has_file = True
            else:
                properties[name] = {"type": _infer_type(entry.get("value"))}
            required.append(name)
        if not properties:
            return None
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        media = (
            "multipart/form-data"
            if (mode == "formdata" or has_file)
            else "application/x-www-form-urlencoded"
        )
        return {"content": {media: {"schema": schema}}}

    if mode == "file":
        return {
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            }
        }

    if mode == "graphql":
        notes.append(
            f"'{item_name}' is a GraphQL request. It was carried over as a JSON body; "
            "GraphQL ingestion is not implemented."
        )
        return {"content": {"application/json": {"schema": {"type": "object"}}}}

    return None


def _schema_from_example(example: Any, depth: int = 0) -> dict:
    """A JSON Schema inferred from one example value.

    Bounded depth: an example is data, and deeply nested data would produce a
    schema nobody reads.
    """
    if depth > 6:
        return {}
    if isinstance(example, dict):
        properties = {
            str(key): _schema_from_example(value, depth + 1) for key, value in example.items()
        }
        return {"type": "object", "properties": properties} if properties else {"type": "object"}
    if isinstance(example, list):
        first = example[0] if example else None
        return {"type": "array", "items": _schema_from_example(first, depth + 1)}
    if example is None:
        return {}
    return {"type": _infer_type(example)}


def _security_from(collection: dict) -> dict | None:
    auth = collection.get("auth")
    if not isinstance(auth, dict):
        return None
    kind = auth.get("type")
    if kind == "bearer":
        return {
            "schemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "requirement": [{"bearerAuth": []}],
            "note": "The collection's bearer auth became an HTTP bearer security scheme.",
        }
    if kind == "basic":
        return {
            "schemes": {"basicAuth": {"type": "http", "scheme": "basic"}},
            "requirement": [{"basicAuth": []}],
            "note": "The collection's basic auth became an HTTP basic security scheme.",
        }
    if kind == "apikey":
        entries = {
            str(entry.get("key")): entry.get("value")
            for entry in auth.get("apikey") or []
            if isinstance(entry, dict)
        }
        name = str(entries.get("key") or "X-Api-Key")
        location = str(entries.get("in") or "header")
        if location not in ("header", "query", "cookie"):
            location = "header"
        return {
            "schemes": {"apiKeyAuth": {"type": "apiKey", "name": name, "in": location}},
            "requirement": [{"apiKeyAuth": []}],
            "note": f"The collection's API key auth became an apiKey scheme in the {location}.",
        }
    if kind == "oauth2":
        return {
            "schemes": {"oauth2Auth": {"type": "oauth2", "flows": {}}},
            "requirement": [{"oauth2Auth": []}],
            "note": (
                "The collection declares OAuth2, but a collection does not record the "
                "authorization or token endpoints — configure them before this scheme can "
                "be used."
            ),
        }
    return None


class PostmanConnector(SourceConnector):
    id = "postman"
    display_name = "Postman collection"
    description = (
        "A Postman collection, converted to OpenAPI. Types are inferred from example "
        "values, because a collection records requests rather than a contract."
    )
    config_fields = (
        ConfigField(
            key="content",
            label="Collection JSON",
            kind="text",
            help="The exported collection (Collection v2.0 or v2.1).",
        ),
    )

    async def connect(self, config: dict, secrets: dict) -> ConnectionResult:
        content = config.get("content")
        if not content:
            return ConnectionResult(connected=False, message="No collection was provided.")
        try:
            document, notes = self._convert(content)
        except ConnectorError as exc:
            return ConnectionResult(connected=False, message=exc.message)
        return ConnectionResult(
            connected=True,
            message=f"Converted {len(document.get('paths') or {})} path(s).",
            detail={"notes": notes},
        )

    async def fetch(self, config: dict, secrets: dict, *, known: Provenance | None = None):
        content = config.get("content")
        if not content:
            raise ConnectorError("no_content", "This source holds no collection.")
        document, notes = self._convert(content)
        return FetchResult(
            content=json.dumps(document, indent=2),
            provenance=Provenance(
                source_type=self.id,
                source_uri=config.get("filename") or "",
                retrieved_at=_now(),
                detail={"conversion_notes": notes},
            ),
        )

    def watch_plan(self, config: dict) -> WatchPlan:
        return WatchPlan(
            mode=WATCH_NONE,
            reason="An uploaded collection cannot change on its own; upload it again to update.",
        )

    def _convert(self, content: str) -> tuple[dict, list[str]]:
        try:
            collection = json.loads(content)
        except ValueError as exc:
            raise ConnectorError("parse_error", f"The collection is not valid JSON: {exc}")
        if not isinstance(collection, dict) or not is_postman_collection(collection):
            raise ConnectorError(
                "not_a_collection",
                "That does not look like a Postman collection. Export it from Postman as "
                "Collection v2.1.",
            )
        return convert_collection(collection)
