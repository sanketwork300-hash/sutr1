"""Standalone MCP server package generator (Sutr spec §28–31).

Turns a compiled tool set into a self-contained, downloadable project:

    <slug>-mcp-server.zip
      server.py         MCP stdio server exposing the tools
      sutr_runtime.py   request building + execution (no Sutr dependency)
      tools.json        the compiled ApiTool definitions + auth config (data)
      test_server.py    generated offline tests (schema + request building)
      requirements.txt / pyproject.toml / Dockerfile / README.md / .env.example

Design rules:
- Generated code is static template text; everything API-specific lives in
  tools.json. The templates never interpolate user-controlled strings into
  Python source, so a hostile spec cannot inject code into the package.
- The runtime mirrors sutr.api_client's request semantics exactly (path
  quoting, query/header wire names, body_param wrapping, auth header merged
  over param headers).
- The base URL is NOT SSRF-screened here: the package runs on the user's own
  machine, where a private-network API is a legitimate target.
- Output is deterministic (fixed zip timestamps) so re-downloads diff cleanly.
"""

import io
import json
import re
import zipfile

from sutr.integrations.types import ApiTool

_ZIP_DATE = (2026, 1, 1, 0, 0, 0)

RUNTIME_PY = '''"""Request runtime for tools compiled from an OpenAPI spec by Sutr.

Self-contained: only stdlib + httpx. All API-specific data lives in
tools.json — this module never needs editing.
"""

import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx

_PATH_PARAM_RE = re.compile(r"\\{(\\w+)\\}")
_MAX_RESPONSE_CHARS = 200_000


def load_bundle(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _path_params(path):
    return _PATH_PARAM_RE.findall(path)


def input_schema(tool):
    """JSON Schema for a tool's arguments (mirrors Sutr's gateway schema)."""
    properties = {}
    required = []
    for p in tool["params"]:
        if p.get("schema_override") is not None:
            properties[p["name"]] = p["schema_override"]
        else:
            prop = {"type": p.get("type", "string")}
            if p.get("description"):
                prop["description"] = p["description"]
            if p.get("default") is not None:
                prop["default"] = p["default"]
            if p.get("enum"):
                prop["enum"] = p["enum"]
            if p.get("type") == "array" and p.get("items"):
                prop["items"] = {"type": p["items"]}
            properties[p["name"]] = prop
        if p.get("required"):
            required.append(p["name"])
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def build_request(bundle, tool, args, token=""):
    """Build the HTTP request for a tool call.

    Returns {"method", "url", "query", "headers", "json_body"}. The auth
    header (when configured and a token is provided) is merged OVER any
    header-typed params, so caller-supplied args can never replace it.
    """
    base_url = bundle["base_url"].rstrip("/")
    path = tool["path"]
    url = base_url + "/" + path.lstrip("/")
    for name in _path_params(path):
        url = url.replace("{" + name + "}", quote(str(args.get(name, "")), safe=""))

    params = tool["params"]
    query = {}
    for p in params:
        if (p.get("query") or p.get("location") == "query") and args.get(p["name"]) is not None:
            query[p.get("wire_name") or p["name"]] = args[p["name"]]

    headers = {}
    for p in params:
        if p.get("location") == "header" and args.get(p["name"]) is not None:
            headers[p.get("wire_name") or p["name"]] = str(args[p["name"]])

    auth = bundle.get("auth") or {}
    if token and auth.get("token_header"):
        fmt = auth.get("token_format") or "{token}"
        headers[auth["token_header"]] = fmt.replace("{token}", token)

    json_body = None
    if tool["method"].upper() in ("POST", "PUT", "PATCH"):
        if tool.get("body_param"):
            json_body = args.get(tool["body_param"])
        else:
            path_names = set(_path_params(path))
            query_names = {
                p["name"] for p in params if p.get("query") or p.get("location") == "query"
            }
            header_names = {p["name"] for p in params if p.get("location") == "header"}
            excluded = path_names | query_names | header_names
            wire = {p["name"]: (p.get("wire_name") or p["name"]) for p in params}
            body = {
                wire.get(k, k): v
                for k, v in args.items()
                if k not in excluded and v is not None
            }
            json_body = body if body else None

    return {
        "method": tool["method"].upper(),
        "url": url,
        "query": query,
        "headers": headers,
        "json_body": json_body,
    }


async def execute_tool(bundle, tool, args, token=""):
    """Execute a tool call. Returns an MCP-style result dict."""
    req = build_request(bundle, tool, args, token)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.request(
                req["method"],
                req["url"],
                params=req["query"] or None,
                json=req["json_body"],
                headers=req["headers"],
            )
    except httpx.HTTPError as exc:
        return {
            "content": [{"type": "text", "text": f"Request failed: {exc}"}],
            "isError": True,
            "status_code": None,
        }

    text = response.text
    if len(text) > _MAX_RESPONSE_CHARS:
        text = text[:_MAX_RESPONSE_CHARS] + "\\n... [truncated]"
    if not text:
        text = f"HTTP {response.status_code} (empty body)"
    return {
        "content": [{"type": "text", "text": text}],
        "isError": response.status_code >= 400,
        "status_code": response.status_code,
    }
'''

SERVER_PY = '''"""Standalone MCP server generated by Sutr from an OpenAPI specification.

Exposes the tools in tools.json over MCP. Credentials come from the
environment variable named in tools.json (never from arguments or files).

Usage:
    python server.py                                  # MCP over stdio (default)
    python server.py --transport http --port 8000     # MCP over streamable HTTP
    python server.py --list-tools                     # print the tools and exit

The HTTP transport serves MCP at /mcp and a plain JSON health probe at
/health (useful for container orchestration).
"""

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from sutr_runtime import execute_tool, input_schema, load_bundle

BUNDLE = load_bundle(Path(__file__).parent / "tools.json")

server = Server(BUNDLE["name"])


def _token() -> str:
    env_var = (BUNDLE.get("auth") or {}).get("env_var") or ""
    return os.environ.get(env_var, "") if env_var else ""


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=input_schema(tool),
        )
        for tool in BUNDLE["tools"]
    ]


@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    tool = next((t for t in BUNDLE["tools"] if t["name"] == name), None)
    if tool is None:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    result = await execute_tool(BUNDLE, tool, arguments or {}, _token())
    return [
        types.TextContent(type="text", text=item.get("text", json.dumps(item)))
        for item in result["content"]
    ]


async def _run_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _build_http_app():
    """Starlette app: MCP streamable HTTP at /mcp, health probe at /health."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def handle_mcp(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    async def health(request):
        return JSONResponse(
            {
                "status": "ok",
                "name": BUNDLE["name"],
                "tools": len(BUNDLE["tools"]),
                "generator": BUNDLE.get("generator"),
            }
        )

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    return Starlette(
        routes=[Route("/health", health), Mount("/mcp", app=handle_mcp)],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-tools", action="store_true", help="Print the available tools and exit"
    )
    parser.add_argument(
        "--transport", choices=["stdio", "http"], default="stdio", help="MCP transport"
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    args = parser.parse_args()

    if args.list_tools:
        for tool in BUNDLE["tools"]:
            print(f"{tool['name']}\\t{tool['method']} {tool['path']}")
        return

    env_var = (BUNDLE.get("auth") or {}).get("env_var")
    if env_var and not os.environ.get(env_var):
        print(
            f"warning: {env_var} is not set - calls to authenticated endpoints will fail",
            flush=True,
        )

    if args.transport == "http":
        import uvicorn

        uvicorn.run(_build_http_app(), host=args.host, port=args.port, log_level="info")
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
'''

TEST_PY = '''"""Generated offline tests for this MCP server package. Run: pytest -q

No network access: they validate the tool bundle, the derived JSON schemas,
and the exact HTTP requests that would be sent.
"""

from pathlib import Path

import sutr_runtime as rt

BUNDLE = rt.load_bundle(Path(__file__).parent / "tools.json")

_SAMPLES = {
    "string": "example",
    "integer": 1,
    "number": 1.5,
    "boolean": True,
    "array": [],
    "object": {},
}


def _sample_args(tool):
    schema = rt.input_schema(tool)
    args = {}
    for name in schema.get("required", []):
        prop = schema["properties"].get(name, {})
        args[name] = _SAMPLES.get(prop.get("type", "string"), "example")
    for name in rt._path_params(tool["path"]):
        args.setdefault(name, "pid-1")
    return args


def test_bundle_shape():
    assert BUNDLE["tools"], "bundle has no tools"
    names = [t["name"] for t in BUNDLE["tools"]]
    assert len(names) == len(set(names)), "duplicate tool names"
    assert BUNDLE["base_url"].startswith(("http://", "https://"))


def test_schemas_generate_for_every_tool():
    for tool in BUNDLE["tools"]:
        schema = rt.input_schema(tool)
        assert schema["type"] == "object"
        for required in schema.get("required", []):
            assert required in schema["properties"]


def test_requests_build_for_every_tool():
    for tool in BUNDLE["tools"]:
        req = rt.build_request(BUNDLE, tool, _sample_args(tool), token="test-token")
        assert req["method"] == tool["method"].upper()
        assert req["url"].startswith(BUNDLE["base_url"].rstrip("/"))
        assert "{" not in req["url"], f"unsubstituted path param in {req['url']}"
        auth = BUNDLE.get("auth") or {}
        if auth.get("token_header"):
            assert auth["token_header"] in req["headers"]


def test_path_params_are_url_encoded():
    tool = next((t for t in BUNDLE["tools"] if rt._path_params(t["path"])), None)
    if tool is None:
        return
    name = rt._path_params(tool["path"])[0]
    args = _sample_args(tool)
    args[name] = "a/b c"
    req = rt.build_request(BUNDLE, tool, args)
    assert "a%2Fb%20c" in req["url"]


def test_args_cannot_override_the_auth_header():
    auth = BUNDLE.get("auth") or {}
    if not auth.get("token_header"):
        return
    synthetic = {
        "name": "synthetic",
        "description": "",
        "method": "GET",
        "path": "/x",
        "params": [{"name": "h", "location": "header", "wire_name": auth["token_header"]}],
        "body_param": None,
    }
    req = rt.build_request(BUNDLE, synthetic, {"h": "attacker-value"}, token="real-token")
    expected = (auth.get("token_format") or "{token}").replace("{token}", "real-token")
    assert req["headers"][auth["token_header"]] == expected
'''

DOCKERFILE = """FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Default is MCP stdio (docker run -i ...). For a network-reachable server,
# pass: --transport http --port 8000
ENTRYPOINT ["python", "server.py"]
"""

# The generated code targets the mcp 1.x server API; 2.0 changed it.
REQUIREMENTS_TXT = """mcp>=1.9.0,<2.0
httpx>=0.27.0
"""

PYPROJECT_TOML = """[project]
name = "__SLUG__"
version = "1.0.0"
description = "Standalone MCP server for __API_TITLE__, generated by Sutr"
requires-python = ">=3.11"
dependencies = [
    "mcp>=1.9.0,<2.0",
    "httpx>=0.27.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0"]
"""

ENV_EXAMPLE = """# API credential for __API_TITLE__ (sent as: __AUTH_PREVIEW__)
__ENV_VAR__=
"""

ENV_EXAMPLE_NO_AUTH = """# This API declares no authentication - no credentials are needed.
"""

README_MD = """# __NAME__ - MCP server

Standalone [MCP](https://modelcontextprotocol.io) server for **__API_TITLE__ \
__API_VERSION__**, generated by [Sutr](https://github.com/sutr-dev/sutr) from the \
API's OpenAPI specification. It exposes __TOOL_COUNT__ tool(s) over MCP stdio and \
proxies calls to `__BASE_URL__`.

## Tools

__TOOL_TABLE__

## Run

```sh
pip install -r requirements.txt
__ENV_LINE__python server.py
```

`python server.py --list-tools` prints the tools without starting the server.

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "__SLUG__": {
      "command": "python",
      "args": ["/absolute/path/to/server.py"]__ENV_JSON__
    }
  }
}
```

### Docker

```sh
docker build -t __SLUG__-mcp .
docker run -i --rm__DOCKER_ENV__ __SLUG__-mcp
```

## Tests

Generated offline tests (no network) validate the tool schemas and the exact
requests the server would send:

```sh
pip install pytest && pytest -q
```

## Notes

- Credentials are read ONLY from the environment variable above - never from
  tool arguments, and agent-supplied headers can never override the
  credential header.
- `tools.json` is data: edit descriptions or delete tools freely; `server.py`
  and `sutr_runtime.py` are generic and need no changes.
"""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "api"


def env_var_for(slug: str) -> str:
    return f"{slug.upper()}_API_TOKEN"


def _tool_table(tools: list[ApiTool]) -> str:
    lines = ["| Tool | Endpoint | Description |", "| --- | --- | --- |"]
    for tool in tools:
        desc = tool.description.split(".")[0][:100].replace("|", "\\|")
        lines.append(f"| `{tool.name}` | `{tool.method} {tool.path}` | {desc} |")
    return "\n".join(lines)


def build_server_package(
    *,
    name: str,
    base_url: str,
    token_header: str,
    token_format: str,
    tools: list[ApiTool],
    api_title: str,
    api_version: str,
) -> tuple[str, bytes]:
    """Generate the package. Returns (zip_filename, zip_bytes)."""
    if not tools:
        raise ValueError("Cannot generate a server package with no tools.")

    slug = slugify(name)
    has_auth = bool(token_header)
    env_var = env_var_for(slug) if has_auth else None

    bundle = {
        "name": name,
        "slug": slug,
        "api_title": api_title,
        "api_version": api_version,
        "base_url": base_url,
        "auth": {
            "token_header": token_header,
            "token_format": token_format or ("{token}" if has_auth else ""),
            "env_var": env_var,
        },
        "generator": "sutr-openapi",
        "tools": [tool.model_dump() for tool in tools],
    }

    auth_preview = (
        f"{token_header}: {(token_format or '{token}').replace('{token}', '<token>')}"
        if has_auth
        else ""
    )
    readme = (
        README_MD.replace("__NAME__", name)
        .replace("__API_TITLE__", api_title)
        .replace("__API_VERSION__", api_version)
        .replace("__TOOL_COUNT__", str(len(tools)))
        .replace("__BASE_URL__", base_url)
        .replace("__TOOL_TABLE__", _tool_table(tools))
        .replace("__SLUG__", slug)
        .replace(
            "__ENV_LINE__", f"export {env_var}=...   # your API credential\n" if env_var else ""
        )
        .replace(
            "__ENV_JSON__",
            (f',\n      "env": {{"{env_var}": "<your token>"}}' if env_var else ""),
        )
        .replace("__DOCKER_ENV__", f" -e {env_var}" if env_var else "")
    )
    env_example = (
        ENV_EXAMPLE.replace("__API_TITLE__", api_title)
        .replace("__AUTH_PREVIEW__", auth_preview)
        .replace("__ENV_VAR__", env_var)
        if env_var
        else ENV_EXAMPLE_NO_AUTH
    )
    pyproject = PYPROJECT_TOML.replace("__SLUG__", slug).replace("__API_TITLE__", api_title)

    files = {
        "README.md": readme,
        "tools.json": json.dumps(bundle, indent=2),
        "sutr_runtime.py": RUNTIME_PY,
        "server.py": SERVER_PY,
        "test_server.py": TEST_PY,
        "requirements.txt": REQUIREMENTS_TXT,
        "pyproject.toml": pyproject,
        "Dockerfile": DOCKERFILE,
        ".env.example": env_example,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename in sorted(files):
            info = zipfile.ZipInfo(filename, date_time=_ZIP_DATE)
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[filename])

    return f"{slug}-mcp-server.zip", buffer.getvalue()
