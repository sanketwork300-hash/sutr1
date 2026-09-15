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
import pathlib
import re
import zipfile

from sutr.integrations.types import ApiTool

_ZIP_DATE = (2026, 1, 1, 0, 0, 0)

# Version of the generated-package templates, recorded on every runtime
# artifact. The LLD requires runtime templates to be versioned independently of
# the IR (§3.6): a fix to the connector changes what is deployed even though
# the specification it was generated from has not changed, and an artifact that
# did not record which template built it could not be told apart from one that
# did.
#
# History:
#   1  stdio/HTTP/SSE server, governance modes, offline test suite
#   2  argument validation, retrying connector with circuit breaker, in-process
#      metrics, non-root container image
TEMPLATE_VERSION = "2"

# The request-building half of the generated runtime is not written here: it
# is `sutr/runtime/request_builder.py`, read verbatim at generation time and
# embedded into the package. ADR-010 — the hosted gateway and a generated
# server must send identical requests, and the only way to guarantee that is
# for them to run the same code rather than two copies someone keeps in sync.
_REQUEST_BUILDER_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "runtime" / "request_builder.py"
)

# The metrics module is a template file for the same reason: code that ships to
# users is easier to read, lint and diff where it is written than inside a
# string constant.
_METRICS_PATH = pathlib.Path(__file__).resolve().parent / "templates" / "sutr_metrics.py"

RUNTIME_HEADER = '''"""Request runtime for tools compiled from an OpenAPI spec by Sutr.

Self-contained: only stdlib + httpx. All API-specific data lives in
tools.json — this module never needs editing.

Three sections:

1. Request construction — the *same source* Sutr's hosted gateway runs,
   embedded here at generation time. Editing it makes this server disagree
   with the platform that generated it.
2. Validation — arguments checked against the tool's declared schema before
   anything leaves this process.
3. The provider connector — timeouts on every phase, bounded retries on
   idempotent methods only, a per-tool circuit breaker, and status codes
   translated into something a caller can act on.
"""

# ── 1. Request construction (shared with Sutr's gateway) ────────────────────

'''

RUNTIME_FOOTER = '''

# ── 2. Validation ───────────────────────────────────────────────────────────

_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_arguments(tool, args):
    """Check a call against the tool's declared schema.

    Returns a list of problems, empty when the call is well formed. This is the
    Validation stage of the middleware chain: it runs before a request is
    built, so a malformed call costs the upstream API nothing and the caller is
    told everything that is wrong at once instead of one field per round trip.

    Unknown arguments are *not* an error. They are ignored during request
    building, and an agent that adds a stray field should not have its call
    refused over something that has no effect.
    """
    problems = []
    schema = tool_input_schema(tool)
    properties = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if (args or {}).get(name) is None:
            problems.append("Missing required argument '%s'." % name)
    for name, value in (args or {}).items():
        declared = properties.get(name) or {}
        expected = declared.get("type")
        python_type = _JSON_TYPES.get(expected)
        if python_type is None or value is None:
            continue
        # bool is a subclass of int in Python. An API that asked for a number
        # did not ask for True.
        if expected in ("integer", "number") and isinstance(value, bool):
            problems.append("Argument '%s' must be of type %s." % (name, expected))
        elif not isinstance(value, python_type):
            problems.append("Argument '%s' must be of type %s." % (name, expected))
    return problems


# ── 3. The provider connector ───────────────────────────────────────────────

from pathlib import Path  # noqa: E402

import asyncio  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402

_MAX_RESPONSE_CHARS = 200_000


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Timeouts are per phase, not one number: a connection that never establishes
# and a server that accepted the request and went quiet are different failures
# and deserve different budgets. No phase is unbounded (LLD §5.8).
CONNECT_TIMEOUT = _env_float("SUTR_CONNECT_TIMEOUT", 10.0)
READ_TIMEOUT = _env_float("SUTR_READ_TIMEOUT", 30.0)
WRITE_TIMEOUT = _env_float("SUTR_WRITE_TIMEOUT", 30.0)
POOL_TIMEOUT = _env_float("SUTR_POOL_TIMEOUT", 5.0)

# Retries apply only to methods HTTP defines as idempotent (RFC 9110 §9.2.2).
# Replaying a POST because a read timed out can charge a card twice, and the
# runtime cannot know whether the request was actually applied.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRIES = _env_int("SUTR_MAX_RETRIES", 2)
RETRY_BACKOFF_SECONDS = _env_float("SUTR_RETRY_BACKOFF_SECONDS", 0.25)

BREAKER_THRESHOLD = _env_int("SUTR_BREAKER_THRESHOLD", 5)
BREAKER_RESET_SECONDS = _env_float("SUTR_BREAKER_RESET_SECONDS", 30.0)

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    """Stop calling a dependency that is already failing (LLD §5.8).

    Three states, and the middle one is the point: after the reset window the
    breaker admits exactly one probe. If that probe fails the breaker opens
    again without the caller having sent a burst of traffic at a service that
    is still down.
    """

    def __init__(self, threshold=None, reset_seconds=None, clock=time.monotonic):
        self.threshold = BREAKER_THRESHOLD if threshold is None else threshold
        self.reset_seconds = BREAKER_RESET_SECONDS if reset_seconds is None else reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at = None
        self._probing = False

    @property
    def state(self):
        if self._opened_at is None:
            return CLOSED
        if self._probing:
            return HALF_OPEN
        if self._clock() - self._opened_at >= self.reset_seconds:
            return HALF_OPEN
        return OPEN

    def allows(self):
        state = self.state
        if state == OPEN:
            return False
        if state == HALF_OPEN:
            self._probing = True
        return True

    def record_success(self):
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self):
        self._probing = False
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = self._clock()

    def retry_after(self):
        """Seconds until the breaker will admit a probe, or 0."""
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.reset_seconds - (self._clock() - self._opened_at))


_BREAKERS = {}


def breaker_for(tool_name):
    """One breaker per tool.

    Per tool rather than per host: a provider whose one slow report endpoint is
    timing out should not have its healthy endpoints cut off, and a tool maps
    one-to-one onto an operation.
    """
    breaker = _BREAKERS.get(tool_name)
    if breaker is None:
        breaker = _BREAKERS[tool_name] = CircuitBreaker()
    return breaker


def reset_breakers():
    _BREAKERS.clear()


# Status → what the caller can actually do about it. An agent reading
# "HTTP 429" often retries immediately; reading that it is rate limited, it
# waits.
_STATUS_HINTS = {
    400: "The API rejected the request as malformed. Check the argument values.",
    401: "The API rejected the credential. Check the token in the environment.",
    403: "The credential is valid but not permitted to perform this operation.",
    404: "The API has no such resource. Check identifiers in the arguments.",
    409: "The request conflicts with the current state of the resource.",
    413: "The request body was too large for the API to accept.",
    415: "The API does not accept the media type this request was sent with.",
    422: "The API understood the request but refused the values in it.",
    429: "The API is rate limiting this credential. Retry later, more slowly.",
    500: "The API failed internally. This is not a problem with the arguments.",
    502: "The API's upstream gateway returned an error.",
    503: "The API is unavailable. It may be restarting or overloaded.",
    504: "The API's gateway timed out waiting for its backend.",
}


def translate_error(status_code):
    """A one-line explanation of a status, or an empty string."""
    hint = _STATUS_HINTS.get(status_code)
    if hint:
        return hint
    if 400 <= status_code < 500:
        return "The API rejected the request."
    if status_code >= 500:
        return "The API failed to handle the request."
    return ""


def _result(text, is_error, status_code, **extra):
    payload = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "status_code": status_code,
    }
    payload.update(extra)
    return payload


def timeout_config():
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=WRITE_TIMEOUT, pool=POOL_TIMEOUT
    )


def load_bundle(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def auth_headers(bundle, token=""):
    """The credential header for this bundle, or nothing when unauthenticated."""
    auth = bundle.get("auth") or {}
    if token and auth.get("token_header"):
        fmt = auth.get("token_format") or "{token}"
        return {auth["token_header"]: fmt.replace("{token}", token)}
    return {}


def build_tool_request(bundle, tool, args, token=""):
    """Build the HTTP request for a tool call, credential included."""
    return build_request(bundle["base_url"], tool, args, auth_headers(bundle, token))


async def execute_tool(bundle, tool, args, token="", client=None, sleep=asyncio.sleep):
    """Execute one tool call: validate → build → send (with retry) → translate.

    `client` is injectable so a caller — including this package's own tests —
    can drive the connector without a network. `sleep` is injectable for the
    same reason: the backoff is real, and a test should not have to wait it out.
    """
    problems = validate_arguments(tool, args or {})
    if problems:
        return _result(" ".join(problems), True, None, error_kind="invalid_arguments")

    try:
        request = build_tool_request(bundle, tool, args, token)
    except RequestBuildError as exc:
        return _result(str(exc), True, None, error_kind="invalid_arguments")

    breaker = breaker_for(tool["name"])
    if not breaker.allows():
        wait = breaker.retry_after()
        return _result(
            "Refused locally: the circuit breaker for '%s' is open after repeated failures. "
            "It will admit a probe in %.0fs." % (tool["name"], wait),
            True,
            None,
            error_kind="circuit_open",
            breaker_state=OPEN,
        )

    method = str(tool.get("method", "GET")).upper()
    retries = MAX_RETRIES if method in IDEMPOTENT_METHODS else 0
    kwargs = request_kwargs(request)
    owns_client = client is None
    attempts = 0
    last_error = None

    if owns_client:
        client = httpx.AsyncClient(timeout=timeout_config(), follow_redirects=False)
    try:
        while True:
            attempts += 1
            try:
                response = await client.request(**kwargs)
            except httpx.HTTPError as exc:
                last_error = "Request failed: %s" % exc
                if attempts <= retries:
                    await sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempts - 1)))
                    continue
                breaker.record_failure()
                return _result(
                    last_error,
                    True,
                    None,
                    error_kind="transport",
                    attempts=attempts,
                    breaker_state=breaker.state,
                )

            if response.status_code in RETRY_STATUSES and attempts <= retries:
                await sleep(RETRY_BACKOFF_SECONDS * (2 ** (attempts - 1)))
                continue
            break
    finally:
        if owns_client:
            await client.aclose()

    if response.status_code >= 500 or response.status_code == 429:
        breaker.record_failure()
    else:
        # A 4xx is the API answering, not the API failing. Counting client
        # errors towards the breaker would let one agent's bad arguments cut
        # off every other caller.
        breaker.record_success()

    text = response.text
    if len(text) > _MAX_RESPONSE_CHARS:
        text = text[:_MAX_RESPONSE_CHARS] + "\\n... [truncated]"
    if not text:
        text = "HTTP %d (empty body)" % response.status_code

    is_error = response.status_code >= 400
    if is_error:
        hint = translate_error(response.status_code)
        if hint:
            text = "HTTP %d - %s\\n\\n%s" % (response.status_code, hint, text)

    return _result(
        text,
        is_error,
        response.status_code,
        error_kind="http_error" if is_error else None,
        attempts=attempts,
        breaker_state=breaker.state,
    )
'''


def render_metrics() -> str:
    """The generated package's `sutr_metrics.py`, read verbatim."""
    return _METRICS_PATH.read_text(encoding="utf-8")


def render_runtime() -> str:
    """Assemble sutr_runtime.py: header + the shared builder + the executor."""
    shared = _REQUEST_BUILDER_PATH.read_text(encoding="utf-8")
    # Drop the shared module's own docstring; the generated file has its own.
    marker = '"""\n\nimport '
    index = shared.find(marker)
    if index != -1:
        shared = shared[index + len('"""\n\n') :]
    return RUNTIME_HEADER + shared + RUNTIME_FOOTER


GOVERNANCE_PY = '''"""Governance mode for this generated MCP server.

A server generated by Sutr runs on *your* infrastructure. Whether the
platform's approval policies apply to it is therefore a deployment decision,
and it is made explicitly rather than assumed (Sutr ADR-007):

    GOVERNANCE_MODE=standalone   (default)
        This deployment is independent. Anyone who can reach the endpoint can
        call its tools. Put it behind your own authentication, or keep it on a
        private network. Sutr's approval policies do NOT apply here — the
        server has no connection to the platform at all.

    GOVERNANCE_MODE=platform
        Every tool call must present a signed access pass issued by the
        platform. The pass is a JWT carrying the tenant, agent, the tools it
        may call, and an expiry. This server validates the signature, issuer,
        audience, expiry, and single-use nonce before executing anything.

Platform mode refuses to start when its validation material is missing. A
governance flag that silently does nothing is worse than no flag at all.

Required in platform mode:
    ACCESS_PASS_ISSUER      expected `iss`
    ACCESS_PASS_AUDIENCE    expected `aud`
    ACCESS_PASS_SECRET      HMAC secret (HS256), or
    ACCESS_PASS_PUBLIC_KEY  PEM public key (RS256/ES256)
"""

import os
import threading
import time

STANDALONE = "standalone"
PLATFORM = "platform"


class GovernanceError(Exception):
    """A pass is missing, invalid, or does not cover the requested tool."""


class ConfigurationError(Exception):
    """Platform mode was requested but cannot be enforced as configured."""


class _NonceCache:
    """Single-use nonces, remembered until the pass they belong to expires.

    In memory, so it is per-process: replay protection holds for one server
    instance. Behind a load balancer with several replicas, a pass could be
    replayed once per replica within its lifetime — which is why passes are
    short-lived. Stated here rather than discovered later.
    """

    def __init__(self):
        self._seen = {}
        self._lock = threading.Lock()

    def claim(self, nonce, expires_at):
        now = time.time()
        with self._lock:
            for key, expiry in list(self._seen.items()):
                if expiry <= now:
                    del self._seen[key]
            if nonce in self._seen:
                return False
            self._seen[nonce] = expires_at
            return True


class Governance:
    def __init__(self, mode, issuer=None, audience=None, secret=None, public_key=None):
        self.mode = mode
        self.issuer = issuer
        self.audience = audience
        self._secret = secret
        self._public_key = public_key
        self._nonces = _NonceCache()

    @property
    def enforced(self):
        return self.mode == PLATFORM

    def describe(self):
        if self.mode == PLATFORM:
            return (
                "governance: platform - every tool call must present a signed access pass "
                f"issued by {self.issuer}"
            )
        return (
            "governance: standalone - this deployment is independent of Sutr's approval "
            "policies. Anyone who can reach this endpoint can call its tools."
        )

    def validate(self, token, tool_name):
        """Validate an access pass and confirm it covers `tool_name`."""
        if not self.enforced:
            return None
        if not token:
            raise GovernanceError("An access pass is required.")

        import jwt

        options = {"require": ["exp", "iss", "aud", "sub"]}
        key = self._secret or self._public_key
        algorithms = ["HS256"] if self._secret else ["RS256", "ES256"]
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=self.audience,
                issuer=self.issuer,
                options=options,
            )
        except Exception as exc:
            raise GovernanceError(f"Access pass rejected: {exc}")

        nonce = claims.get("nonce")
        if not nonce:
            raise GovernanceError("Access pass carries no nonce.")
        if not self._nonces.claim(nonce, float(claims["exp"])):
            raise GovernanceError("Access pass has already been used.")

        if not claims.get("tenant_id"):
            raise GovernanceError("Access pass carries no tenant.")

        allowed = claims.get("tools")
        if allowed is not None and tool_name not in allowed:
            raise GovernanceError(f"Access pass does not cover the tool '{tool_name}'.")
        return claims


def load(env=None):
    """Build the governance configuration from the environment."""
    env = env if env is not None else os.environ
    mode = (env.get("GOVERNANCE_MODE") or STANDALONE).strip().lower()
    if mode not in (STANDALONE, PLATFORM):
        raise ConfigurationError(
            f"GOVERNANCE_MODE must be '{STANDALONE}' or '{PLATFORM}', not {mode!r}."
        )
    if mode == STANDALONE:
        return Governance(STANDALONE)

    issuer = env.get("ACCESS_PASS_ISSUER")
    audience = env.get("ACCESS_PASS_AUDIENCE")
    secret = env.get("ACCESS_PASS_SECRET")
    public_key = env.get("ACCESS_PASS_PUBLIC_KEY")
    missing = [
        name
        for name, value in (
            ("ACCESS_PASS_ISSUER", issuer),
            ("ACCESS_PASS_AUDIENCE", audience),
        )
        if not value
    ]
    if not secret and not public_key:
        missing.append("ACCESS_PASS_SECRET or ACCESS_PASS_PUBLIC_KEY")
    if missing:
        raise ConfigurationError(
            "GOVERNANCE_MODE=platform requires " + ", ".join(missing) + ". "
            "Refusing to start: a governance mode that cannot verify anything would "
            "report as enforced while enforcing nothing."
        )
    try:
        import jwt  # noqa: F401
    except ImportError:
        raise ConfigurationError(
            "GOVERNANCE_MODE=platform needs PyJWT. Install it with: pip install pyjwt"
        )
    return Governance(PLATFORM, issuer, audience, secret, public_key)
'''

SERVER_PY = '''"""Standalone MCP server generated by Sutr from an OpenAPI specification.

Exposes the tools in tools.json over MCP. Credentials come from the
environment variable named in tools.json (never from arguments or files).

Usage:
    python server.py                                  # MCP over stdio (default)
    python server.py --transport http --port 8000     # MCP over streamable HTTP
    python server.py --transport sse  --port 8000     # MCP over SSE
    python server.py --list-tools                     # print the tools and exit

Transports:
    stdio  the default; for clients that launch a subprocess
    http   MCP streamable HTTP at /mcp
    sse    GET /sse for the event stream, POST /messages for client messages

Every HTTP transport also serves a plain JSON health probe at /health, which
is what container orchestrators poll, plus /metrics in the Prometheus text
format and /metrics.json for anything that would rather read JSON.

Governance: see sutr_governance.py. By default this deployment is
INDEPENDENT of Sutr's approval policies; set GOVERNANCE_MODE=platform to
require a signed access pass on every call.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from contextvars import ContextVar
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

import sutr_governance
import sutr_metrics
from sutr_runtime import execute_tool, load_bundle, tool_input_schema

BUNDLE = load_bundle(Path(__file__).parent / "tools.json")

server = Server(BUNDLE["name"])

# The validated access pass for the request being handled, or None in
# standalone mode. Bound by the HTTP middleware before the MCP session sees
# the request.
_current_pass: ContextVar = ContextVar("_current_pass", default=None)
GOVERNANCE = sutr_governance.Governance(sutr_governance.STANDALONE)


def _token() -> str:
    env_var = (BUNDLE.get("auth") or {}).get("env_var") or ""
    return os.environ.get(env_var, "") if env_var else ""


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=tool["name"],
            description=tool["description"],
            inputSchema=tool_input_schema(tool),
        )
        for tool in BUNDLE["tools"]
    ]


def _finish(name, outcome, started, status_code=None, attempts=1):
    """The Logging and Metrics stages, which always run together.

    One structured line per call, on stderr — stdout carries the MCP protocol
    itself on the stdio transport, so writing a log line there would corrupt
    the session. The line is emitted once the outcome is known rather than
    twice per call: a "received" line and a "finished" line say the same thing
    about the same call, and only the second one is informative.
    """
    duration = time.monotonic() - started
    sutr_metrics.METRICS.record(
        name, outcome, duration, status_code=status_code, attempts=attempts
    )
    print(
        json.dumps(
            {
                "event": "tool_call",
                "tool": name,
                "outcome": outcome,
                "status_code": status_code,
                "attempts": attempts,
                "duration_ms": round(duration * 1000, 3),
            }
        ),
        file=sys.stderr,
        flush=True,
    )


def _refuse(name, outcome, started, message):
    _finish(name, outcome, started)
    return [types.TextContent(type="text", text=message)]


@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """The generated middleware chain.

    Authentication → Validation → Logging → Execution → Metrics → Response.

    Authentication ran in `_guard` before the transport saw the request; what
    remains here is the part that is per-call and could not run earlier —
    whether the pass covers *this* tool. Validation lives in `sutr_runtime`
    beside the schema it validates against, and runs first inside
    `execute_tool`. This function is the order those stages run in.
    """
    started = time.monotonic()
    tool = next((t for t in BUNDLE["tools"] if t["name"] == name), None)
    if tool is None:
        return _refuse(name, "unknown_tool", started, f"Unknown tool: {name}")
    if GOVERNANCE.enforced:
        claims = _current_pass.get()
        if claims is None:
            return _refuse(
                name, "no_access_pass", started, "Refused: no valid access pass for this request."
            )
        allowed = claims.get("tools")
        if allowed is not None and name not in allowed:
            return _refuse(
                name,
                "pass_out_of_scope",
                started,
                f"Refused: the access pass does not cover the tool '{name}'.",
            )
    result = await execute_tool(BUNDLE, tool, arguments or {}, _token())
    outcome = result.get("error_kind") or ("error" if result.get("isError") else "ok")
    _finish(
        name,
        outcome,
        started,
        status_code=result.get("status_code"),
        attempts=result.get("attempts", 1),
    )
    return [
        types.TextContent(type="text", text=item.get("text", json.dumps(item)))
        for item in result["content"]
    ]


async def _run_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _bearer(scope) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == b"authorization":
            raw = value.decode("latin-1")
            if raw.lower().startswith("bearer "):
                return raw[7:].strip()
    return ""


def _guard(app):
    """Validate the access pass before the MCP transport sees the request.

    In standalone mode this is a pass-through, so the two modes share one code
    path and the enforced one cannot rot from disuse.
    """

    async def guarded(scope, receive, send):
        if scope["type"] != "http" or not GOVERNANCE.enforced:
            await app(scope, receive, send)
            return
        try:
            claims = GOVERNANCE.validate(_bearer(scope), tool_name=None)
        except sutr_governance.GovernanceError as exc:
            body = json.dumps({"error": "access_pass_rejected", "message": str(exc)}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"content-length", str(len(body)).encode()],
                        [b"www-authenticate", b"Bearer"],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token = _current_pass.set(claims)
        try:
            await app(scope, receive, send)
        finally:
            _current_pass.reset(token)

    return guarded


def _build_http_app(transport: str = "http"):
    """Starlette app for an HTTP transport, plus a health probe at /health."""
    from mcp.server.sse import SseServerTransport
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse, Response
    from starlette.routing import Mount, Route

    async def health(request):
        return JSONResponse(
            {
                "status": "ok",
                "name": BUNDLE["name"],
                "tools": len(BUNDLE["tools"]),
                "generator": BUNDLE.get("generator"),
                "transport": transport,
                "governance": GOVERNANCE.mode,
            }
        )

    async def metrics(request):
        """Prometheus scrape endpoint.

        Left unauthenticated on purpose: it exposes call counts and latencies,
        never arguments, responses or credentials, and a scrape target that
        needs a token is a scrape target nobody configures. Keep it off the
        public internet with a NetworkPolicy or an ingress rule, the same way
        every other container's /metrics is kept off it.
        """
        return PlainTextResponse(
            sutr_metrics.METRICS.prometheus_text(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def metrics_json(request):
        return JSONResponse(sutr_metrics.METRICS.snapshot())

    if transport == "sse":
        sse = SseServerTransport("/messages")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as (read_stream, write_stream):
                await server.run(
                    read_stream, write_stream, server.create_initialization_options()
                )
            return Response()

        return Starlette(
            routes=[
                Route("/health", health),
                Route("/metrics", metrics),
                Route("/metrics.json", metrics_json),
                Route("/sse", endpoint=_guard(handle_sse), methods=["GET"]),
                Mount("/messages", app=_guard(sse.handle_post_message)),
            ]
        )

    manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def handle_mcp(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/metrics", metrics),
            Route("/metrics.json", metrics_json),
            Mount("/mcp", app=_guard(handle_mcp)),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-tools", action="store_true", help="Print the available tools and exit"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="MCP transport",
    )
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    args = parser.parse_args()

    if args.list_tools:
        for tool in BUNDLE["tools"]:
            print(f"{tool['name']}\\t{tool['method']} {tool['path']}")
        return

    global GOVERNANCE
    try:
        GOVERNANCE = sutr_governance.load()
    except sutr_governance.ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)

    if GOVERNANCE.enforced and args.transport == "stdio":
        print(
            "error: GOVERNANCE_MODE=platform needs an HTTP transport - a stdio session "
            "carries no per-call access pass. Use --transport http or --transport sse.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print(GOVERNANCE.describe(), flush=True)

    env_var = (BUNDLE.get("auth") or {}).get("env_var")
    if env_var and not os.environ.get(env_var):
        print(
            f"warning: {env_var} is not set - calls to authenticated endpoints will fail",
            flush=True,
        )

    if args.transport in ("http", "sse"):
        import uvicorn

        uvicorn.run(
            _build_http_app(args.transport), host=args.host, port=args.port, log_level="info"
        )
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
    schema = rt.tool_input_schema(tool)
    args = {}
    for name in schema.get("required", []):
        prop = schema["properties"].get(name, {})
        args[name] = _SAMPLES.get(prop.get("type", "string"), "example")
    for name in rt.path_param_names(tool["path"]):
        args.setdefault(name, "pid-1")
    return args


def test_bundle_shape():
    assert BUNDLE["tools"], "bundle has no tools"
    names = [t["name"] for t in BUNDLE["tools"]]
    assert len(names) == len(set(names)), "duplicate tool names"
    assert BUNDLE["base_url"].startswith(("http://", "https://"))


def test_schemas_generate_for_every_tool():
    for tool in BUNDLE["tools"]:
        schema = rt.tool_input_schema(tool)
        assert schema["type"] == "object"
        for required in schema.get("required", []):
            assert required in schema["properties"]


def test_requests_build_for_every_tool():
    for tool in BUNDLE["tools"]:
        req = rt.build_tool_request(BUNDLE, tool, _sample_args(tool), token="test-token")
        assert req["method"] == tool["method"].upper()
        assert req["url"].startswith(BUNDLE["base_url"].rstrip("/"))
        assert "{" not in req["url"], f"unsubstituted path param in {req['url']}"
        auth = BUNDLE.get("auth") or {}
        if auth.get("token_header"):
            assert auth["token_header"] in req["headers"]


def test_path_params_are_url_encoded():
    tool = next((t for t in BUNDLE["tools"] if rt.path_param_names(t["path"])), None)
    if tool is None:
        return
    name = rt.path_param_names(tool["path"])[0]
    args = _sample_args(tool)
    args[name] = "a/b c"
    req = rt.build_tool_request(BUNDLE, tool, args)
    assert "a%2Fb%20c" in req["url"]


def test_every_tool_declares_a_known_body_encoding():
    for tool in BUNDLE["tools"]:
        assert tool.get("body_encoding", "json") in rt.ENCODINGS


def test_bodies_are_built_for_the_declared_encoding():
    for tool in BUNDLE["tools"]:
        req = rt.build_tool_request(BUNDLE, tool, _sample_args(tool))
        kind = req["body"]["kind"]
        assert kind in rt.ENCODINGS
        if tool["method"].upper() in ("GET", "HEAD"):
            assert kind == rt.NONE, f"{tool['name']} would send a body on a GET"


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
        "body_encoding": "none",
    }
    req = rt.build_tool_request(BUNDLE, synthetic, {"h": "attacker-value"}, token="real-token")
    expected = (auth.get("token_format") or "{token}").replace("{token}", "real-token")
    assert req["headers"][auth["token_header"]] == expected
'''

# A minimal base and a non-root, read-only application directory. This is the
# part of the LLD's supply-chain requirement (§3.6) that a generator can
# actually deliver: image signing, an SBOM attached to the *image*, and a
# vulnerability scan all need a registry and a scanner, which is why the
# artifact's SBOM and signature live on the platform side instead (ADR-038).
#
# The base is deliberately NOT pinned to a digest: a digest names one exact
# build, and pinning to one this generator has never pulled would assert a
# provenance it cannot back. Pin it yourself in a regulated environment.
DOCKERFILE = """FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Nothing in this server writes to disk, so the code it runs is made
# unwritable and the process that runs it is not root. A compromised tool call
# then cannot rewrite the server it was served by.
RUN useradd --system --uid 10001 --no-create-home mcp && chmod -R a-w /app
USER 10001

EXPOSE 8000

# Default is MCP stdio (docker run -i ...). For a network-reachable server,
# pass: --transport http --port 8000
ENTRYPOINT ["python", "server.py"]
"""

# The generated code targets the mcp 1.x server API; 2.0 changed it.
REQUIREMENTS_TXT = """mcp>=1.9.0,<2.0
httpx>=0.27.0
# Only needed when GOVERNANCE_MODE=platform, which validates signed access passes.
pyjwt>=2.8.0
uvicorn>=0.30.0
"""

PYPROJECT_TOML = """[project]
name = "__SLUG__"
version = "1.0.0"
description = "Standalone MCP server for __API_TITLE__, generated by Sutr"
requires-python = ">=3.11"
dependencies = [
    "mcp>=1.9.0,<2.0",
    "httpx>=0.27.0",
    "uvicorn>=0.30.0",
]

[project.optional-dependencies]
# Validating signed access passes (GOVERNANCE_MODE=platform).
platform = ["pyjwt>=2.8.0"]
dev = ["pytest>=8.0.0"]
"""

_GOVERNANCE_ENV = """
# Governance (see sutr_governance.py).
#   standalone (default) - this deployment is independent of Sutr's approval
#                          policies; anyone who can reach it can call its tools
#   platform             - every call must present a signed access pass
GOVERNANCE_MODE=standalone
# Required only when GOVERNANCE_MODE=platform:
# ACCESS_PASS_ISSUER=
# ACCESS_PASS_AUDIENCE=
# ACCESS_PASS_SECRET=
"""

ENV_EXAMPLE = (
    """# API credential for __API_TITLE__ (sent as: __AUTH_PREVIEW__)
__ENV_VAR__=
"""
    + _GOVERNANCE_ENV
)

ENV_EXAMPLE_NO_AUTH = (
    """# This API declares no authentication - no credentials are needed.
"""
    + _GOVERNANCE_ENV
)

README_MD = """# __NAME__ - MCP server

Standalone [MCP](https://modelcontextprotocol.io) server for **__API_TITLE__ \
__API_VERSION__**, generated by [Sutr](https://github.com/sutr-dev/sutr) from the \
API's OpenAPI specification. It exposes __TOOL_COUNT__ tool(s) over MCP \
(stdio, streamable HTTP, or SSE) and proxies calls to `__BASE_URL__`.

## Tools

__TOOL_TABLE__

## Run

```sh
pip install -r requirements.txt
__ENV_LINE__python server.py
```

`python server.py --list-tools` prints the tools without starting the server.

### Transports

| Transport | Command | Endpoints |
| --- | --- | --- |
| stdio (default) | `python server.py` | - |
| streamable HTTP | `python server.py --transport http` | `POST /mcp`, `GET /health` |
| SSE | `python server.py --transport sse` | `GET /sse`, `POST /messages`, `GET /health` |

Both HTTP transports take `--host` and `--port`.

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

## Governance - read this before deploying

This server runs on **your** infrastructure. What that means for access control
is stated plainly rather than implied:

### `GOVERNANCE_MODE=standalone` (the default)

This deployment is **independent of Sutr**. It does not call the platform, and
the per-tool approval policies configured in your Sutr console **do not apply
to it**. Anyone who can reach the endpoint can call every tool, using the
credential baked into the deployment's environment.

That is a perfectly reasonable way to run it - on a private network, behind
your own gateway, or over stdio on a single machine. It is only a problem if
you assumed otherwise, which is why it is written here.

### `GOVERNANCE_MODE=platform`

Every tool call must present a signed **access pass** as
`Authorization: Bearer <pass>`. The server validates its signature, issuer,
audience, expiry, single-use nonce, tenant, and the list of tools the pass
covers, before executing anything.

```sh
export GOVERNANCE_MODE=platform
export ACCESS_PASS_ISSUER=https://your-sutr-host
export ACCESS_PASS_AUDIENCE=https://this-runtime/mcp
export ACCESS_PASS_SECRET=...        # or ACCESS_PASS_PUBLIC_KEY for RS256/ES256
pip install pyjwt
python server.py --transport http --port 8000
```

The server **refuses to start** if platform mode is requested without the
values it needs to verify a pass - a governance mode that verifies nothing
would report as enforced while enforcing nothing. Platform mode also requires
an HTTP transport: a stdio session carries no per-call header, so there is
nowhere for a pass to travel.

Replay protection is per process. Behind several replicas, a pass could be
replayed once per replica within its lifetime, which is why passes are
short-lived.

## Notes

- Credentials are read ONLY from the environment variable above - never from
  tool arguments, and agent-supplied headers can never override the
  credential header.
- `tools.json` is data: edit descriptions or delete tools freely; `server.py`,
  `sutr_runtime.py` and `sutr_governance.py` are generic and need no changes.
- `sutr_runtime.py` contains the same request-building code Sutr's hosted
  gateway runs, embedded at generation time - so this server and the platform
  send identical requests for identical arguments.
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


def build_server_files(
    *,
    name: str,
    base_url: str,
    token_header: str,
    token_format: str,
    tools: list[ApiTool],
    api_title: str,
    api_version: str,
    knowledge: dict | None = None,
) -> dict[str, str]:
    """Generate the package's files as text. Returns {filename: content}.

    Separate from `build_server_package` because the generator needs to look at
    what it built — compile it, scan it, list it in an SBOM — and unzipping
    something it just zipped to do that would be silly. The zip is the delivery
    format, not the artifact.

    `knowledge` is the documentation knowledge folded into this build (LLD
    §3.6: *IR + documentation knowledge + template → MCP server*). It is
    written into tools.json alongside the tools, so a generated server carries
    its provider's rules and vocabulary with it and an agent reading the tool
    list sees them.
    """
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
        "template_version": TEMPLATE_VERSION,
        "tools": [tool.model_dump() for tool in tools],
    }
    if knowledge:
        bundle["knowledge"] = knowledge

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

    return {
        "README.md": readme,
        "tools.json": json.dumps(bundle, indent=2, sort_keys=True),
        "sutr_runtime.py": render_runtime(),
        "sutr_governance.py": GOVERNANCE_PY,
        "sutr_metrics.py": render_metrics(),
        "server.py": SERVER_PY,
        "test_server.py": TEST_PY,
        "requirements.txt": REQUIREMENTS_TXT,
        "pyproject.toml": pyproject,
        "Dockerfile": DOCKERFILE,
        ".env.example": env_example,
    }


def zip_files(files: dict[str, str]) -> bytes:
    """Zip a file map deterministically.

    Fixed timestamps and fixed permissions, entries in sorted order: the same
    files must produce the same bytes, or the artifact's build hash would
    change every time it was rebuilt and "identical inputs ⇒ identical outputs"
    would be untestable.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename in sorted(files):
            info = zipfile.ZipInfo(filename, date_time=_ZIP_DATE)
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[filename])
    return buffer.getvalue()


def build_server_package(
    *,
    name: str,
    base_url: str,
    token_header: str,
    token_format: str,
    tools: list[ApiTool],
    api_title: str,
    api_version: str,
    knowledge: dict | None = None,
) -> tuple[str, bytes]:
    """Generate the package. Returns (zip_filename, zip_bytes)."""
    files = build_server_files(
        name=name,
        base_url=base_url,
        token_header=token_header,
        token_format=token_format,
        tools=tools,
        api_title=api_title,
        api_version=api_version,
        knowledge=knowledge,
    )
    return f"{slugify(name)}-mcp-server.zip", zip_files(files)
