"""The generated provider connector: validation, timeouts, retries, breaker, metrics.

LLD §3.6 asks for a *"provider connector per operation with retry, timeout,
circuit-breaker hooks, error translation"* and a middleware chain that includes
Validation and Metrics. These tests load the generated modules the way a user
would — from an extracted package — and drive them against a stub transport, so
what is tested is the code that actually ships.
"""

import importlib.util
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

from sutr.integrations.types import ApiTool, Param
from sutr.openapi.packaging import build_server_package

TOOLS = [
    ApiTool(
        name="get_pet",
        description="Get a pet. HTTP GET /pets/{pet_id}.",
        method="GET",
        path="/pets/{pet_id}",
        params=[Param(name="pet_id", type="string", required=True, location="path")],
    ),
    ApiTool(
        name="create_pet",
        description="Create a pet. HTTP POST /pets.",
        method="POST",
        path="/pets",
        params=[
            Param(name="name", type="string", required=True, location="body"),
            Param(name="count", type="integer", location="body"),
        ],
    ),
]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="package")
def package_fixture(tmp_path) -> Path:
    _filename, data = build_server_package(
        name="Petstore Tools",
        base_url="https://api.petstore.example.com/v1",
        token_header="X-Api-Key",
        token_format="{token}",
        tools=TOOLS,
        api_title="Petstore",
        api_version="1.2.0",
    )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(tmp_path)
    return tmp_path


@pytest.fixture(name="runtime")
def runtime_fixture(package):
    module = _load(package / "sutr_runtime.py", "gen_runtime_connector")
    module.reset_breakers()
    # Backoff is real; a test should not have to wait it out.
    module.RETRY_BACKOFF_SECONDS = 0
    return module


@pytest.fixture(name="bundle")
def bundle_fixture(package):
    return json.loads((package / "tools.json").read_text())


def _tool(bundle, name):
    return next(tool for tool in bundle["tools"] if tool["name"] == name)


async def _noop_sleep(_seconds):
    return None


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── Validation ───────────────────────────────────────────────────────────────


def test_a_missing_required_argument_is_named(runtime, bundle):
    problems = runtime.validate_arguments(_tool(bundle, "get_pet"), {})
    assert problems == ["Missing required argument 'pet_id'."]


def test_a_wrong_type_is_named(runtime, bundle):
    problems = runtime.validate_arguments(_tool(bundle, "create_pet"), {"name": 5})
    assert problems == ["Argument 'name' must be of type string."]


def test_a_boolean_is_not_a_number(runtime, bundle):
    problems = runtime.validate_arguments(
        _tool(bundle, "create_pet"), {"name": "Rex", "count": True}
    )
    assert problems == ["Argument 'count' must be of type integer."]


def test_an_unknown_argument_is_ignored_rather_than_refused(runtime, bundle):
    """It has no effect on the request; refusing the call over it would be worse."""
    assert runtime.validate_arguments(_tool(bundle, "create_pet"), {"name": "Rex", "x": 1}) == []


async def test_an_invalid_call_never_reaches_the_network(runtime, bundle):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    result = await runtime.execute_tool(
        bundle, _tool(bundle, "get_pet"), {}, "token", client=_client(handler)
    )
    assert result["isError"] is True
    assert result["error_kind"] == "invalid_arguments"
    assert calls == []


# ── Timeouts ─────────────────────────────────────────────────────────────────


def test_every_phase_of_a_request_has_its_own_bounded_timeout(runtime):
    timeout = runtime.timeout_config()
    assert timeout.connect == runtime.CONNECT_TIMEOUT
    assert timeout.read == runtime.READ_TIMEOUT
    assert None not in (timeout.connect, timeout.read, timeout.write, timeout.pool)


# ── Retries ──────────────────────────────────────────────────────────────────


async def test_a_retriable_status_on_an_idempotent_method_is_retried(runtime, bundle):
    statuses = [503, 503, 200]

    def handler(request):
        return httpx.Response(statuses.pop(0), text="ok")

    result = await runtime.execute_tool(
        bundle,
        _tool(bundle, "get_pet"),
        {"pet_id": "1"},
        "token",
        client=_client(handler),
        sleep=_noop_sleep,
    )
    assert result["status_code"] == 200
    assert result["attempts"] == 3


async def test_a_post_is_never_retried(runtime, bundle):
    """Replaying a POST after a timeout can charge a card twice."""
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503, text="down")

    result = await runtime.execute_tool(
        bundle,
        _tool(bundle, "create_pet"),
        {"name": "Rex"},
        "token",
        client=_client(handler),
        sleep=_noop_sleep,
    )
    assert len(attempts) == 1
    assert result["attempts"] == 1


async def test_a_client_error_is_not_retried(runtime, bundle):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(404, text="no such pet")

    await runtime.execute_tool(
        bundle,
        _tool(bundle, "get_pet"),
        {"pet_id": "1"},
        "token",
        client=_client(handler),
        sleep=_noop_sleep,
    )
    assert len(attempts) == 1


async def test_a_transport_failure_is_retried_then_reported(runtime, bundle):
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("refused")

    result = await runtime.execute_tool(
        bundle,
        _tool(bundle, "get_pet"),
        {"pet_id": "1"},
        "token",
        client=_client(handler),
        sleep=_noop_sleep,
    )
    assert len(attempts) == runtime.MAX_RETRIES + 1
    assert result["error_kind"] == "transport"


# ── Circuit breaker ──────────────────────────────────────────────────────────


def test_the_breaker_opens_after_repeated_failures_and_half_opens_later(runtime):
    now = [0.0]
    breaker = runtime.CircuitBreaker(threshold=2, reset_seconds=30, clock=lambda: now[0])
    assert breaker.allows()
    breaker.record_failure()
    assert breaker.state == runtime.CLOSED
    breaker.record_failure()
    assert breaker.state == runtime.OPEN
    assert breaker.allows() is False

    now[0] = 31.0
    assert breaker.state == runtime.HALF_OPEN
    assert breaker.allows() is True
    # One probe, not a burst: the probe failing re-opens it.
    breaker.record_failure()
    assert breaker.state == runtime.OPEN


def test_a_success_closes_the_breaker(runtime):
    breaker = runtime.CircuitBreaker(threshold=1, reset_seconds=1)
    breaker.record_failure()
    assert breaker.state == runtime.OPEN
    breaker.record_success()
    assert breaker.state == runtime.CLOSED


async def test_an_open_breaker_refuses_locally_without_calling_the_api(runtime, bundle):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, text="boom")

    tool = _tool(bundle, "get_pet")
    for _ in range(runtime.BREAKER_THRESHOLD):
        await runtime.execute_tool(
            bundle, tool, {"pet_id": "1"}, "t", client=_client(handler), sleep=_noop_sleep
        )
    before = len(calls)
    result = await runtime.execute_tool(
        bundle, tool, {"pet_id": "1"}, "t", client=_client(handler), sleep=_noop_sleep
    )
    assert len(calls) == before
    assert result["error_kind"] == "circuit_open"
    assert "circuit breaker" in result["content"][0]["text"]


async def test_a_client_error_does_not_open_the_breaker(runtime, bundle):
    """One agent's bad arguments must not cut off every other caller."""

    def handler(request):
        return httpx.Response(400, text="bad")

    tool = _tool(bundle, "create_pet")
    for _ in range(runtime.BREAKER_THRESHOLD + 2):
        result = await runtime.execute_tool(
            bundle, tool, {"name": "Rex"}, "t", client=_client(handler), sleep=_noop_sleep
        )
    assert result["error_kind"] == "http_error"
    assert runtime.breaker_for("create_pet").state == runtime.CLOSED


def test_breakers_are_per_tool(runtime):
    assert runtime.breaker_for("a") is not runtime.breaker_for("b")
    assert runtime.breaker_for("a") is runtime.breaker_for("a")


# ── Error translation ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (401, "rejected the credential"),
        (403, "not permitted"),
        (404, "no such resource"),
        (429, "rate limiting"),
        (500, "failed internally"),
    ],
)
async def test_a_status_is_translated_into_something_actionable(runtime, bundle, status, phrase):
    def handler(request):
        return httpx.Response(status, text="upstream body")

    result = await runtime.execute_tool(
        bundle,
        _tool(bundle, "create_pet"),
        {"name": "Rex"},
        "t",
        client=_client(handler),
        sleep=_noop_sleep,
    )
    text = result["content"][0]["text"]
    assert phrase in text
    # The upstream body is still there: the hint is added, never substituted.
    assert "upstream body" in text


async def test_a_successful_response_is_returned_unchanged(runtime, bundle):
    def handler(request):
        return httpx.Response(200, text='{"id": "1"}')

    result = await runtime.execute_tool(
        bundle,
        _tool(bundle, "get_pet"),
        {"pet_id": "1"},
        "t",
        client=_client(handler),
        sleep=_noop_sleep,
    )
    assert result["isError"] is False
    assert result["content"][0]["text"] == '{"id": "1"}'
    assert result["error_kind"] is None


# ── Metrics ──────────────────────────────────────────────────────────────────


def test_metrics_count_calls_and_expose_prometheus_text(package):
    metrics = _load(package / "sutr_metrics.py", "gen_metrics")
    recorder = metrics.Metrics()
    recorder.record("get_pet", "ok", 0.02, status_code=200)
    recorder.record("get_pet", "http_error", 1.5, status_code=500, attempts=3)
    recorder.record("create_pet", "circuit_open", 0.0)

    snapshot = recorder.snapshot()
    assert snapshot["tools"]["get_pet"]["calls"] == 2
    assert snapshot["retries"] == 2
    assert snapshot["circuit_open_refusals"] == 1

    text = recorder.prometheus_text()
    assert 'mcp_tool_calls_total{tool="get_pet",outcome="ok"} 1' in text
    assert 'mcp_tool_responses_total{tool="get_pet",status="500"} 1' in text
    assert "mcp_tool_retries_total 2" in text
    assert "# TYPE mcp_tool_duration_seconds histogram" in text


def test_histogram_buckets_are_cumulative(package):
    metrics = _load(package / "sutr_metrics.py", "gen_metrics_buckets")
    recorder = metrics.Metrics()
    for duration in (0.01, 0.2, 3.0):
        recorder.record("t", "ok", duration)
    lines = [line for line in recorder.prometheus_text().splitlines() if "_bucket" in line]
    counts = [int(line.rsplit(" ", 1)[1]) for line in lines]
    assert counts == sorted(counts)
    assert counts[-1] == 3


def test_a_label_value_with_a_quote_is_escaped(package):
    metrics = _load(package / "sutr_metrics.py", "gen_metrics_escape")
    recorder = metrics.Metrics()
    recorder.record('we"ird', "ok", 0.1)
    assert 'tool="we\\"ird"' in recorder.prometheus_text()
