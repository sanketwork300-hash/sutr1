"""The single definition of how a compiled tool becomes an HTTP request.

ADR-010: Sutr executes compiled tools two ways — the hosted gateway proxies
them dynamically, and a generated standalone package runs them on the user's
own machine. Those are two runtimes, but they must produce *byte-identical*
requests, or a tool that works in the playground breaks after deployment.

This module is that shared definition. It is deliberately dependency-free —
stdlib only, no `sutr` imports, no httpx — because `openapi/packaging.py`
embeds this exact file into every generated package. The generated runtime is
not a hand-maintained copy that resembles this one; it *is* this one.

Anything provider-specific, credential-shaped, or policy-shaped belongs
elsewhere: this module turns (tool definition, arguments, auth headers) into a
request description and nothing more.
"""

import base64
import binascii
import json
import re
from urllib.parse import quote

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")

# Body encodings a compiled tool can carry. `none` means the operation takes
# no request body at all.
JSON = "json"
FORM = "form"
MULTIPART = "multipart"
BINARY = "binary"
TEXT = "text"
NONE = "none"

ENCODINGS = (JSON, FORM, MULTIPART, BINARY, TEXT, NONE)

DEFAULT_CONTENT_TYPES = {
    JSON: "application/json",
    FORM: "application/x-www-form-urlencoded",
    MULTIPART: "multipart/form-data",
    BINARY: "application/octet-stream",
    TEXT: "text/plain",
}


class RequestBuildError(ValueError):
    """An argument cannot be encoded for the request (e.g. malformed base64)."""


# ── schema ───────────────────────────────────────────────────────────────────


def input_schema(params) -> dict:
    """JSON Schema for a tool's arguments."""
    properties: dict = {}
    required: list = []
    for param in params:
        override = param.get("schema_override")
        if override is not None:
            properties[param["name"]] = override
        else:
            prop: dict = {"type": param.get("type", "string")}
            if param.get("description"):
                prop["description"] = param["description"]
            if param.get("default") is not None:
                prop["default"] = param["default"]
            if param.get("enum"):
                prop["enum"] = param["enum"]
            if param.get("type") == "array" and param.get("items"):
                prop["items"] = {"type": param["items"]}
            properties[param["name"]] = prop
        if param.get("required"):
            required.append(param["name"])
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def tool_input_schema(tool) -> dict:
    """JSON Schema for a whole tool definition."""
    return input_schema(tool.get("params") or [])


# ── parameter placement ──────────────────────────────────────────────────────


def path_param_names(path: str):
    return _PATH_PARAM_RE.findall(path)


def build_url(base_url: str, path: str, args: dict) -> str:
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    for name in path_param_names(path):
        value = args.get(name, "")
        url = url.replace("{" + name + "}", quote(str(value), safe=""))
    return url


def build_query(params, args: dict) -> dict:
    query = {}
    for param in params:
        if not (param.get("query") or param.get("location") == "query"):
            continue
        value = args.get(param["name"])
        if value is not None:
            query[param.get("wire_name") or param["name"]] = value
    return query


def build_param_headers(params, args: dict) -> dict:
    """Headers the tool declares as parameters.

    These are merged *under* the auth headers by `build_request`, so an agent
    can never replace the integration's credential header with an argument.
    """
    headers = {}
    for param in params:
        if param.get("location") != "header":
            continue
        value = args.get(param["name"])
        if value is not None:
            headers[param.get("wire_name") or param["name"]] = str(value)
    return headers


def is_binary_param(param) -> bool:
    override = param.get("schema_override") or {}
    return override.get("format") == "binary"


def body_param_names(tool) -> list:
    """Arguments that belong in the request body.

    Explicit `location: "body"` wins; otherwise anything not consumed by the
    path, query, or headers falls through to the body, which is the behaviour
    hand-built integrations have always had.
    """
    params = tool.get("params") or []
    explicit = [p["name"] for p in params if p.get("location") == "body"]
    if explicit:
        return explicit
    consumed = set(path_param_names(tool["path"]))
    for param in params:
        if param.get("query") or param.get("location") in ("query", "header"):
            consumed.add(param["name"])
    return [p["name"] for p in params if p["name"] not in consumed]


def _wire_names(tool) -> dict:
    return {p["name"]: (p.get("wire_name") or p["name"]) for p in (tool.get("params") or [])}


def _param_by_name(tool, name):
    for param in tool.get("params") or []:
        if param["name"] == name:
            return param
    return None


# ── body ─────────────────────────────────────────────────────────────────────


def _decode_binary(value, where: str) -> bytes:
    """Interpret an argument as bytes.

    MCP tool arguments are JSON, so binary content arrives base64-encoded —
    there is no other honest way to carry bytes through a JSON schema, and the
    generated tool description says so. Raw bytes are accepted too, for a
    caller that has them.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str):
        raise RequestBuildError(f"{where} must be a base64-encoded string.")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RequestBuildError(f"{where} is not valid base64: {exc}")


def build_body(tool, args: dict) -> dict:
    """Build the request body description for a tool call.

    Returns `{"kind": ...}` plus the payload for that kind. `kind` is one of
    the ENCODINGS values; `none` means no body is sent.
    """
    method = str(tool.get("method", "GET")).upper()
    encoding = tool.get("body_encoding") or JSON
    if encoding == NONE:
        return {"kind": NONE}
    # GET/DELETE/HEAD bodies are legal but almost never intended; the compiler
    # only ever sets a body encoding on operations whose spec declares one.
    if method in ("GET", "HEAD"):
        return {"kind": NONE}

    single = tool.get("body_param")
    wire = _wire_names(tool)

    if encoding == BINARY:
        if not single:
            return {"kind": NONE}
        value = args.get(single)
        if value is None:
            return {"kind": NONE}
        return {"kind": BINARY, "content": _decode_binary(value, f"Argument '{single}'")}

    if encoding == TEXT:
        if not single:
            return {"kind": NONE}
        value = args.get(single)
        if value is None:
            return {"kind": NONE}
        text = value if isinstance(value, str) else json.dumps(value)
        return {"kind": TEXT, "content": text}

    if encoding in (FORM, MULTIPART):
        names = body_param_names(tool)
        data = {}
        files = {}
        for name in names:
            value = args.get(name)
            if value is None:
                continue
            param = _param_by_name(tool, name)
            key = wire.get(name, name)
            if encoding == MULTIPART and param is not None and is_binary_param(param):
                files[key] = (key, _decode_binary(value, f"Argument '{name}'"))
                continue
            if isinstance(value, (dict, list)):
                # A structured value in a form body has no standard encoding;
                # JSON is the least surprising and is what most APIs accept.
                data[key] = json.dumps(value)
            elif isinstance(value, bool):
                data[key] = "true" if value else "false"
            else:
                data[key] = str(value)
        if not data and not files:
            return {"kind": NONE}
        if encoding == MULTIPART:
            return {"kind": MULTIPART, "data": data, "files": files}
        return {"kind": FORM, "data": data}

    # JSON (the default).
    if single:
        value = args.get(single)
        return {"kind": JSON, "json": value} if value is not None else {"kind": NONE}
    body = {}
    for name in body_param_names(tool):
        value = args.get(name)
        if value is not None:
            body[wire.get(name, name)] = value
    return {"kind": JSON, "json": body} if body else {"kind": NONE}


# ── credentials ──────────────────────────────────────────────────────────────

# Where a credential can be placed on the wire. OpenAPI's `apiKey` scheme
# allows all three; `http` schemes are always a header.
HEADER = "header"
QUERY = "query"
COOKIE = "cookie"

PLACEMENTS = (HEADER, QUERY, COOKIE)


def apply_credentials(headers: dict, query: dict, credentials) -> None:
    """Place resolved credentials into the request, in place.

    A credential is `{"location", "name", "format", "value"}`. `format` carries
    the literal `{token}` slot, so a bearer scheme is `"Bearer {token}"` and a
    bare API key is `"{token}"`.

    Credentials are applied *after* arguments and overwrite them: an agent must
    never be able to replace an integration's credential by naming a parameter
    after it. Cookie credentials are merged into a single Cookie header, since
    that is how cookies travel and appending a second header would drop one.
    """
    cookies = []
    for credential in credentials or []:
        value = credential.get("value")
        if not value:
            continue
        rendered = (credential.get("format") or "{token}").replace("{token}", value)
        if "\r" in rendered or "\n" in rendered:
            raise RequestBuildError("A credential value must not contain newlines.")
        location = credential.get("location") or HEADER
        name = credential.get("name")
        if not name:
            continue
        if location == QUERY:
            query[name] = rendered
        elif location == COOKIE:
            cookies.append(f"{name}={rendered}")
        else:
            headers[name] = rendered
    if cookies:
        existing = headers.get("Cookie")
        headers["Cookie"] = "; ".join(([existing] if existing else []) + cookies)


# ── the whole request ────────────────────────────────────────────────────────


def build_request(
    base_url: str,
    tool: dict,
    args: dict,
    auth_headers: dict | None = None,
    credentials=None,
) -> dict:
    """Turn a tool definition plus arguments into a request description.

    Returns {"method", "url", "query", "headers", "body"}. Both runtimes call
    this and only this; see the module docstring.

    `auth_headers` is the single-header form every hand-built integration uses.
    `credentials` is the general form, which additionally reaches query strings
    and cookies and can carry several schemes at once (ADR-009).
    """
    args = args or {}
    params = tool.get("params") or []

    headers = build_param_headers(params, args)
    # Auth last so it wins over anything an argument tried to set.
    for key, value in (auth_headers or {}).items():
        headers[key] = value

    body = build_body(tool, args)
    content_type = tool.get("body_content_type")
    if body["kind"] in (BINARY, TEXT):
        # httpx cannot infer a content type for a raw body, and the spec told
        # us exactly which one the operation expects.
        headers.setdefault("Content-Type", content_type or DEFAULT_CONTENT_TYPES[body["kind"]])
    elif body["kind"] == JSON and content_type and content_type != DEFAULT_CONTENT_TYPES[JSON]:
        # e.g. application/merge-patch+json — preserve what the spec declared.
        headers.setdefault("Content-Type", content_type)

    query = build_query(params, args)
    apply_credentials(headers, query, credentials)

    return {
        "method": str(tool.get("method", "GET")).upper(),
        "url": build_url(base_url, tool["path"], args),
        "query": query,
        "headers": headers,
        "body": body,
    }


def request_kwargs(request: dict) -> dict:
    """Translate a request description into httpx request keyword arguments."""
    body = request["body"]
    kwargs = {
        "method": request["method"],
        "url": request["url"],
        "params": request["query"] or None,
        "headers": request["headers"],
    }
    kind = body["kind"]
    if kind == JSON:
        kwargs["json"] = body["json"]
    elif kind == FORM:
        kwargs["data"] = body["data"]
    elif kind == MULTIPART:
        kwargs["data"] = body["data"]
        kwargs["files"] = body["files"]
        # httpx sets the multipart boundary itself; a caller-supplied
        # Content-Type would be missing it and the request would fail.
        kwargs["headers"] = {
            k: v for k, v in request["headers"].items() if k.lower() != "content-type"
        }
    elif kind in (BINARY, TEXT):
        kwargs["content"] = body["content"]
    return kwargs
