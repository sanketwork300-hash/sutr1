"""Python SDK tests against a scripted transport (httpx.MockTransport).

The interesting behaviour is the approval flow and the error mapping, so those
get the most attention; the parity test at the bottom guards the invariant that
makes this SDK maintainable — sync and async must expose the same surface.
"""

import httpx
import pytest

from sutr_sdk import (
    ApprovalPending,
    ApprovalRequired,
    AsyncSutr,
    AuthenticationError,
    NotFoundError,
    PermissionDenied,
    RateLimited,
    ServerError,
    Sutr,
    SutrConnectionError,
    SutrError,
    ToolDenied,
)

BASE = "https://sutr.test"
KEY = "ap_testkey"

TOOL_RESULT = {"content": [{"type": "text", "text": "done"}], "isError": False, "duration_ms": 12}
APPROVAL_BODY = {
    "error": "approval_required",
    "approval_request_id": "11111111-1111-1111-1111-111111111111",
    "approval_url": f"{BASE}/approve/11111111-1111-1111-1111-111111111111",
    "message": "Tool call requires approval before execution.",
    "integration_id": "posthog",
    "tool_name": "create_annotation",
}


class Recorder:
    """Scripted handler: records requests, replays queued responses."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if self.queue else httpx.Response(200, json={})
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def make_client(*responses, **kwargs) -> tuple[Sutr, Recorder]:
    recorder = Recorder(*responses)
    client = Sutr(KEY, base_url=BASE, **kwargs)
    client._http = httpx.Client(transport=httpx.MockTransport(recorder), headers=client._headers)
    return client, recorder


def make_async_client(*responses, **kwargs) -> tuple[AsyncSutr, Recorder]:
    recorder = Recorder(*responses)
    client = AsyncSutr(KEY, base_url=BASE, **kwargs)
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder), headers=client._headers
    )
    return client, recorder


# ── Construction & auth ──────────────────────────────────────────────────────


def test_requires_credentials(monkeypatch):
    monkeypatch.delenv("SUTR_API_KEY", raising=False)
    with pytest.raises(SutrError, match="No credentials"):
        Sutr()


def test_reads_credentials_and_url_from_env(monkeypatch):
    monkeypatch.setenv("SUTR_API_KEY", "ap_env")
    monkeypatch.setenv("SUTR_BASE_URL", "https://env.example.com/")
    client = Sutr()
    assert client.base_url == "https://env.example.com"  # trailing slash trimmed
    assert client._headers["X-API-Key"] == "ap_env"


def test_api_key_sent_as_header():
    client, recorder = make_client(httpx.Response(200, json=[]))
    client.list_tools()
    assert recorder.requests[0].headers["x-api-key"] == KEY
    assert "sutr-sdk-python" in recorder.requests[0].headers["user-agent"]


def test_bearer_token_alternative():
    client = Sutr(access_token="jwt-token", base_url=BASE)
    assert client._headers["Authorization"] == "Bearer jwt-token"
    assert "X-API-Key" not in client._headers


def test_explicit_access_token_beats_env_api_key(monkeypatch):
    """An ambient SUTR_API_KEY must not override an explicit user token — a key
    cannot read user-scoped endpoints, so silently swapping it breaks logs()."""
    monkeypatch.setenv("SUTR_API_KEY", "ap_ambient")
    client = Sutr(access_token="jwt-token", base_url=BASE)
    assert client._headers["Authorization"] == "Bearer jwt-token"
    assert "X-API-Key" not in client._headers


def test_env_api_key_still_used_when_nothing_passed(monkeypatch):
    monkeypatch.setenv("SUTR_API_KEY", "ap_ambient")
    assert Sutr(base_url=BASE)._headers["X-API-Key"] == "ap_ambient"


# ── Tools ────────────────────────────────────────────────────────────────────


def test_list_tools_parses_and_stamps_integration():
    payload = [
        {
            "name": "create_annotation",
            "description": "Create one",
            "inputSchema": {
                "type": "object",
                "properties": {"content": {}},
                "required": ["content"],
            },
            "execution_mode": "require_approval",
        }
    ]
    client, recorder = make_client(httpx.Response(200, json=payload))
    tools = client.list_tools("posthog")

    assert recorder.paths == ["/api/tools/posthog"]
    assert [t.name for t in tools] == ["create_annotation"]
    assert tools[0].integration_id == "posthog"  # stamped from the argument
    assert tools[0].required_params == ["content"]
    assert tools[0].raw["description"] == "Create one"


def test_list_all_tools_uses_collection_endpoint():
    client, recorder = make_client(httpx.Response(200, json=[]))
    client.list_tools()
    assert recorder.paths == ["/api/tools"]


def test_integration_ids_are_url_encoded():
    client, recorder = make_client(httpx.Response(200, json=TOOL_RESULT))
    client.call_tool("weird/id space", "t", {})
    # url.path is decoded by httpx, so assert on the wire form.
    assert recorder.requests[0].url.raw_path == b"/api/tools/weird%2Fid%20space/call"


def test_call_tool_returns_result_with_text_helper():
    client, recorder = make_client(httpx.Response(200, json=TOOL_RESULT))
    result = client.call_tool("posthog", "create_annotation", {"content": "hi"})

    assert result.text == "done"
    assert result.is_error is False
    assert result.duration_ms == 12
    body = recorder.requests[0].read().decode()
    assert '"tool_name":"create_annotation"' in body.replace(" ", "")


def test_additional_info_is_forwarded():
    client, recorder = make_client(httpx.Response(200, json=TOOL_RESULT))
    client.call_tool("posthog", "t", {}, additional_info="because reasons")
    assert "because reasons" in recorder.requests[0].read().decode()


def test_additional_info_omitted_when_absent():
    client, recorder = make_client(httpx.Response(200, json=TOOL_RESULT))
    client.call_tool("posthog", "t", {})
    assert "additional_info" not in recorder.requests[0].read().decode()


def test_multiple_text_blocks_join():
    payload = {"content": [{"text": "a"}, {"type": "image"}, {"text": "b"}]}
    client, _ = make_client(httpx.Response(200, json=payload))
    assert client.call_tool("i", "t").text == "a\nb"


# ── Governance responses ─────────────────────────────────────────────────────


def test_approval_required_raises_with_url_and_id():
    client, _ = make_client(httpx.Response(403, json=APPROVAL_BODY))
    with pytest.raises(ApprovalRequired) as exc:
        client.call_tool("posthog", "create_annotation", {"content": "hi"})

    assert exc.value.approval_request_id == APPROVAL_BODY["approval_request_id"]
    assert exc.value.approval_url.endswith(APPROVAL_BODY["approval_request_id"])
    assert exc.value.tool_name == "create_annotation"


def test_denied_raises_tool_denied():
    body = {"error": "denied", "message": "This tool has been blocked.", "tool_name": "nuke"}
    client, _ = make_client(httpx.Response(403, json=body))
    with pytest.raises(ToolDenied) as exc:
        client.call_tool("posthog", "nuke")
    assert exc.value.tool_name == "nuke"


def test_permission_denied_is_distinct_from_tool_denied():
    """A role-based 403 must not be mistaken for a policy denial."""
    body = {"detail": {"error": "permission_denied", "message": "Your role does not allow this."}}
    client, _ = make_client(httpx.Response(403, json=body))
    with pytest.raises(PermissionDenied) as exc:
        client.call_tool("posthog", "t")
    assert "role" in exc.value.message


def test_wait_for_approval_polls_then_retries_the_call():
    client, recorder = make_client(
        httpx.Response(403, json=APPROVAL_BODY),  # gated
        httpx.Response(200, json={**APPROVAL_BODY, "status": "pending", "message": "waiting"}),
        httpx.Response(200, json={**APPROVAL_BODY, "status": "approved", "message": "ok"}),
        httpx.Response(200, json=TOOL_RESULT),  # retried call succeeds
    )
    result = client.call_tool(
        "posthog", "create_annotation", {"content": "hi"}, wait_for_approval=True
    )

    assert result.text == "done"
    assert recorder.paths == [
        "/api/tools/posthog/call",
        f"/api/tool-approvals/requests/{APPROVAL_BODY['approval_request_id']}/await",
        f"/api/tool-approvals/requests/{APPROVAL_BODY['approval_request_id']}/await",
        "/api/tools/posthog/call",
    ]
    # The retry must be byte-identical, or the approval's arg hash won't match.
    assert recorder.requests[0].read() == recorder.requests[3].read()


def test_wait_for_approval_raises_on_denial():
    client, _ = make_client(
        httpx.Response(403, json=APPROVAL_BODY),
        httpx.Response(
            200, json={**APPROVAL_BODY, "status": "denied", "message": "Denied by human"}
        ),
    )
    with pytest.raises(SutrError, match="Denied by human"):
        client.call_tool("posthog", "create_annotation", {}, wait_for_approval=True)


def test_wait_for_approval_timeout_raises_approval_pending():
    pending = {**APPROVAL_BODY, "status": "pending", "message": "waiting"}
    # The mock transport answers instantly, so the deadline can permit more than
    # one poll; queue enough that the outcome is deterministic either way.
    client, _ = make_client(
        httpx.Response(403, json=APPROVAL_BODY),
        *[httpx.Response(200, json=pending) for _ in range(5)],
    )
    with pytest.raises(ApprovalPending) as exc:
        client.call_tool(
            "posthog", "create_annotation", {}, wait_for_approval=True, approval_timeout=0.01
        )
    assert exc.value.approval_request_id == APPROVAL_BODY["approval_request_id"]


def test_pending_polls_are_rate_floored(monkeypatch):
    """A server that answers 'pending' instantly must not become a hot loop."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    pending = {**APPROVAL_BODY, "status": "pending", "message": "waiting"}
    client, recorder = make_client(
        httpx.Response(200, json=pending),
        httpx.Response(200, json={**APPROVAL_BODY, "status": "approved", "message": "ok"}),
    )
    decision = client.await_approval(APPROVAL_BODY["approval_request_id"])

    assert decision.approved
    assert len(recorder.requests) == 2
    assert slept and 0 < slept[0] <= 0.5  # spacing enforced between polls


def test_await_approval_sends_remaining_window():
    client, recorder = make_client(
        httpx.Response(200, json={**APPROVAL_BODY, "status": "approved", "message": "ok"})
    )
    decision = client.await_approval(APPROVAL_BODY["approval_request_id"], timeout=30)
    assert decision.approved
    assert "timeout_seconds" in recorder.requests[0].read().decode()


def test_await_approval_without_timeout_sends_no_window():
    client, recorder = make_client(
        httpx.Response(200, json={**APPROVAL_BODY, "status": "approved", "message": "ok"})
    )
    client.await_approval(APPROVAL_BODY["approval_request_id"])
    assert recorder.requests[0].read() in (b"", b"null")


# ── Error mapping ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthenticationError),
        (404, NotFoundError),
        (429, RateLimited),
        (500, ServerError),
        (503, ServerError),
    ],
)
def test_status_codes_map_to_errors(status, expected):
    # max_retries=0 so transient codes surface instead of being retried away
    # (retry behaviour has its own tests below).
    client, _ = make_client(httpx.Response(status, json={"detail": "boom"}), max_retries=0)
    with pytest.raises(expected) as exc:
        client.list_tools()
    assert exc.value.status_code == status
    assert exc.value.message == "boom"


def test_request_id_is_attached_for_support():
    client, _ = make_client(
        httpx.Response(
            500, json={"detail": "Internal Server Error"}, headers={"X-Request-ID": "abc123"}
        )
    )
    with pytest.raises(ServerError) as exc:
        client.list_tools()
    assert exc.value.request_id == "abc123"
    assert "abc123" in str(exc.value)


def test_validation_errors_are_summarised():
    body = {"detail": [{"loc": ["body", "tool_name"], "msg": "field required"}]}
    client, _ = make_client(httpx.Response(422, json=body))
    with pytest.raises(SutrError, match="tool_name: field required"):
        client.call_tool("i", "t")


def test_connection_failures_are_wrapped():
    client, _ = make_client(httpx.ConnectError("refused"))
    with pytest.raises(SutrConnectionError, match="Could not reach"):
        client.list_tools()


def test_non_json_error_body_is_tolerated():
    client, _ = make_client(httpx.Response(502, text="<html>bad gateway</html>"), max_retries=0)
    with pytest.raises(ServerError):
        client.list_tools()


# ── Retries ──────────────────────────────────────────────────────────────────


def test_reads_retry_transient_failures(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    client, recorder = make_client(
        httpx.Response(503, json={"detail": "unavailable"}),
        httpx.Response(200, json=[]),
    )
    assert client.list_tools() == []
    assert len(recorder.requests) == 2


def test_tool_calls_are_never_retried(monkeypatch):
    """A retried tool call could execute twice — side effects are not idempotent."""
    monkeypatch.setattr("time.sleep", lambda _s: None)
    client, recorder = make_client(
        httpx.Response(503, json={"detail": "unavailable"}),
        httpx.Response(200, json=TOOL_RESULT),
    )
    with pytest.raises(ServerError):
        client.call_tool("posthog", "create_annotation", {})
    assert len(recorder.requests) == 1


def test_retries_give_up_after_max(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    client, recorder = make_client(
        *[httpx.Response(503, json={"detail": "nope"}) for _ in range(5)], max_retries=1
    )
    with pytest.raises(ServerError):
        client.list_tools()
    assert len(recorder.requests) == 2  # original + one retry


# ── Other endpoints ──────────────────────────────────────────────────────────


def test_integrations_merges_installed_state():
    catalog = [
        {"id": "posthog", "name": "PostHog", "type": "remote_mcp", "auth": [{"method": "token"}]},
        {"id": "github", "name": "GitHub", "type": "remote_mcp", "auth": [{"method": "oauth"}]},
    ]
    installed = [{"integration_id": "posthog", "connected": True}]
    client, recorder = make_client(
        httpx.Response(200, json=catalog), httpx.Response(200, json=installed)
    )
    result = client.integrations()

    assert recorder.paths == ["/api/integrations", "/api/installed"]
    by_id = {i.id: i for i in result}
    assert by_id["posthog"].installed and by_id["posthog"].connected
    assert not by_id["github"].installed
    assert by_id["github"].auth_methods == ["oauth"]


def test_usage_and_logs_pass_filters():
    client, recorder = make_client(
        httpx.Response(200, json={"tool_calls": 7}),
        httpx.Response(200, json=[]),
    )
    assert client.usage(start="2026-01-01T00:00:00Z")["tool_calls"] == 7
    client.logs(integration="posthog", outcome="executed", limit=5)

    assert recorder.requests[0].url.params["start"] == "2026-01-01T00:00:00Z"
    params = recorder.requests[1].url.params
    assert params["integration"] == "posthog"
    assert params["outcome"] == "executed"
    assert params["limit"] == "5"
    assert "tool" not in params  # None filters are dropped, not sent as "None"


def test_context_manager_closes():
    client, _ = make_client(httpx.Response(200, json=[]))
    with client as c:
        c.list_tools()
    assert client._http.is_closed


# ── Async client ─────────────────────────────────────────────────────────────


async def test_async_call_tool():
    client, recorder = make_async_client(httpx.Response(200, json=TOOL_RESULT))
    async with client:
        result = await client.call_tool("posthog", "create_annotation", {"content": "hi"})
    assert result.text == "done"
    assert recorder.paths == ["/api/tools/posthog/call"]


async def test_async_approval_flow():
    client, recorder = make_async_client(
        httpx.Response(403, json=APPROVAL_BODY),
        httpx.Response(200, json={**APPROVAL_BODY, "status": "approved", "message": "ok"}),
        httpx.Response(200, json=TOOL_RESULT),
    )
    async with client:
        result = await client.call_tool("posthog", "create_annotation", {}, wait_for_approval=True)
    assert result.text == "done"
    assert len(recorder.requests) == 3


async def test_async_raises_same_errors():
    client, _ = make_async_client(httpx.Response(401, json={"detail": "bad key"}))
    async with client:
        with pytest.raises(AuthenticationError):
            await client.list_tools()


async def test_async_retries_reads(monkeypatch):
    async def no_sleep(_s):
        return None

    monkeypatch.setattr("asyncio.sleep", no_sleep)
    client, recorder = make_async_client(
        httpx.Response(502, json={"detail": "gateway"}), httpx.Response(200, json=[])
    )
    async with client:
        assert await client.list_tools() == []
    assert len(recorder.requests) == 2


def test_sync_and_async_expose_the_same_surface():
    """Guards the reason the shared core exists: the two clients must not drift."""
    public = lambda cls: {  # noqa: E731
        name for name in vars(cls) if not name.startswith("_") and callable(getattr(cls, name))
    }
    sync_only = public(Sutr) - public(AsyncSutr) - {"close"}
    async_only = public(AsyncSutr) - public(Sutr) - {"aclose"}
    assert sync_only == set()
    assert async_only == set()
