"""MCP gateway surface.

Thin by design: upstream tool execution goes through
`services.tool_pipeline` — the exact pipeline the REST surface uses — so
policy, approvals, logging, and analytics can never drift between the two.
This file translates MCP protocol frames in and TextContent out, and owns the
MCP-only concerns: the meta-tool surface, session registration for
list_changed pushes, and the approval long-poll.
"""

import json
import logging
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlmodel import Session, select

from sutr.approvals import events as approval_events
from sutr.approvals.requests import try_consume_approved_request
from sutr.authz import ensure_agent_can
from sutr.config import settings
from sutr.db import engine
from sutr.mcp import management_tools
from sutr.mcp.notifications import register_session
from sutr.models.integration import InstalledIntegration
from sutr.models.tool_approval_request import ToolApprovalRequest
from sutr.services.tool_pipeline import (
    CallContext,
    ExecutionOutcome,
    GateResult,
    evaluate_gate,
    execute_tool,
    find_gate_log,
)

logger = logging.getLogger(__name__)

# Injected per-request by the ASGI wrapper before handle_request runs.
_current_auth: ContextVar = ContextVar("_current_auth")


@dataclass
class RequestMeta:
    ip: str | None
    user_agent: str | None


_current_request_meta: ContextVar[RequestMeta | None] = ContextVar(
    "_current_request_meta", default=None
)

mcp_server = Server("Sutr")

# Advertise `tools.listChanged: true` in the initialize handshake so clients
# know to honour `notifications/tools/list_changed` pushes. The SDK's session
# manager calls create_initialization_options() with no arguments, so we patch
# it on this instance to set tools_changed=True.
_default_create_init_options = mcp_server.create_initialization_options


def _create_init_options_with_list_changed(
    notification_options: NotificationOptions | None = None,
    experimental_capabilities: dict | None = None,
):
    return _default_create_init_options(
        notification_options or NotificationOptions(tools_changed=True),
        experimental_capabilities or {},
    )


mcp_server.create_initialization_options = _create_init_options_with_list_changed


@mcp_server.list_tools()
async def _list_tools() -> list[types.Tool]:
    auth = _current_auth.get()
    org_id = auth.org.id

    # Register this MCP session under its org so tool-list mutations elsewhere
    # can push notifications/tools/list_changed to it. Safe to call repeatedly.
    try:
        register_session(org_id, mcp_server.request_context.session)
    except LookupError:
        # Running outside a request context (shouldn't happen here) — skip.
        pass

    # The MCP surface is deliberately narrow: the meta tools are the only
    # top-level tools. Upstream integration tools are discovered via
    # sutr__list_integration_tools and invoked via sutr__call_tool.
    return list(management_tools.MANAGEMENT_TOOLS)


@mcp_server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    auth = _current_auth.get()

    if name.startswith(management_tools.MANAGEMENT_PREFIX):
        return await management_tools.dispatch(name, arguments, auth)

    return [
        types.TextContent(
            type="text",
            text=(
                f"Unknown tool: {name}. Invoke upstream integration tools via "
                "sutr__call_tool(integration_id, tool_name, arguments)."
            ),
        )
    ]


def _pipeline_context(additional_info: str | None = None) -> CallContext:
    """Build the pipeline CallContext from this request's MCP contextvars."""
    auth = _current_auth.get()
    meta = _current_request_meta.get()
    return CallContext.from_agent_auth(
        auth,
        source="mcp",
        requester_ip=meta.ip if meta else None,
        user_agent=meta.user_agent if meta else None,
        additional_info=additional_info,
    )


def _text(message: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=message)]


def _outcome_to_contents(outcome: ExecutionOutcome) -> list[types.TextContent]:
    if outcome.failure == "integration_not_found":
        return _text(outcome.error or "Integration not found.")
    if outcome.failure == "tool_not_found":
        return _text(outcome.error or "Tool not found.")
    if outcome.error is not None:
        return _text(f"Tool call failed: {outcome.error}")

    contents: list[types.TextContent] = []
    result = outcome.result
    if result and "content" in result:
        for item in result["content"]:
            text = item.get("text", json.dumps(item))
            contents.append(types.TextContent(type="text", text=text))
    if not contents:
        contents.append(types.TextContent(type="text", text=json.dumps(result)))
    return contents


async def execute_upstream_tool(
    integration_id: str, tool_name: str, arguments: dict
) -> list[types.TextContent]:
    """Run an integration tool through the canonical pipeline.

    Used by the sutr__call_tool meta-tool. Relies on _current_auth and
    _current_request_meta being set by the ASGI wrapper for this request.
    """
    if not isinstance(arguments, dict):
        arguments = {}

    # Optional free-text rationale from the agent. Pop so it doesn't get forwarded
    # to the upstream tool, whose schema may reject unknown fields.
    additional_info: str | None = None
    if "additional_info" in arguments:
        raw = arguments.pop("additional_info")
        if isinstance(raw, str) and raw.strip():
            additional_info = raw

    ctx = _pipeline_context(additional_info)

    with Session(engine) as session:
        # Viewers may browse tools but never execute them (API keys carry no
        # role and keep their documented capabilities).
        try:
            ensure_agent_can(session, _current_auth.get(), "tools:execute")
        except HTTPException:
            return _text("Your role does not allow executing tools (tools:execute).")

        installed = session.exec(
            select(InstalledIntegration)
            .where(InstalledIntegration.org_id == ctx.org_id)
            .where(InstalledIntegration.integration_id == integration_id)
        ).first()
        if not installed:
            return _text(f"Integration '{integration_id}' not found.")

        gate = evaluate_gate(session, ctx, integration_id, tool_name, arguments)

    if gate.status == "rate_limited":
        return _text(
            "Rate limit reached for this organization's tool calls. Wait "
            f"{gate.retry_after or 60} seconds before trying again — and if you are "
            "looping, stop and reconsider the plan."
        )

    if gate.status == "denied":
        return _text("This tool has been blocked and cannot be executed.")

    if gate.status == "approval_pending":
        return _text(
            "Sutr is a gateway for human-in-the-loop tool calling. "
            "This tool was marked by a human as needing approval. "
            "Share this URL with a human and explain what you were trying to do "
            "(don't assume they can read this response): "
            f"{gate.approval_url}\n\n"
            "Then, without waiting for a chat reply from the human, call "
            "sutr__await_approval(request_id="
            f'"{gate.approval_request_id}") to be notified as soon as they decide. '
            "If it returns 'still pending', call it again until you get a "
            "decision or the human tells you to stop."
        )

    outcome = await execute_tool(ctx, integration_id, tool_name, arguments, gate)
    return _outcome_to_contents(outcome)


async def await_approval(request_id: uuid.UUID) -> list[types.TextContent]:
    """Long-poll for a decision on `request_id`.

    Returns:
    - The upstream tool result when the request is approved (executes the
      tool using the args stored on the ToolApprovalRequest).
    - A denied-by-human message on deny.
    - A still-pending message on timeout (agent can loop back in).

    Ownership is enforced: the request must belong to the caller's org,
    matching the existing /api/tool-approvals/requests/{id} behavior.
    """
    auth = _current_auth.get()
    org = auth.org

    # Initial ownership + state check so we 404 fast on bad ids.
    with Session(engine) as session:
        req = session.get(ToolApprovalRequest, request_id)
        if not req or req.org_id != org.id:
            return _text(f"Approval request '{request_id}' not found.")
        initial_status = req.status
        integration_id = req.integration_id
        tool_name = req.tool_name
        args_json = req.args_json
        args_hash = req.args_hash
        additional_info = req.additional_info

    # If a decision is already in the DB (e.g. approve/deny raced ahead of our
    # first call, or this is a retry after a timeout/server restart), skip the
    # wait and resolve directly.
    if initial_status in ("pending",):

        def _peek_status() -> str | None:
            with Session(engine) as session:
                current = session.get(ToolApprovalRequest, request_id)
                return current.status if current else None

        decision = await approval_events.wait_for_decision(
            request_id,
            timeout=float(settings.approval_long_poll_timeout_seconds),
            pre_check=_peek_status,
        )

        if decision == "timeout":
            return _text(
                "Still pending — the human hasn't decided yet. Call "
                f'sutr__await_approval(request_id="{request_id}") again to '
                "keep waiting, or stop if they need more time."
            )
        if decision == "denied":
            return _text("This tool call was denied by the human.")
        # decision == "approved": fall through to execute
    elif initial_status == "denied":
        return _text("This tool call was denied by the human.")
    elif initial_status != "approved":
        # expired / consumed / auto_approved: nothing to wait on and nothing
        # reasonable to re-execute here — tell the agent to restart the call.
        return _text(
            f"Approval request '{request_id}' is '{initial_status}' and cannot be "
            "awaited. Retry the original sutr__call_tool call to start over."
        )

    # Re-read to tolerate server-restart recovery: the expires_at cut-off may
    # have fired during the wait, or a concurrent /deny may have won.
    with Session(engine) as session:
        req = session.get(ToolApprovalRequest, request_id)
        if not req or req.org_id != org.id:
            return _text(f"Approval request '{request_id}' not found.")
        if req.status == "denied":
            return _text("This tool call was denied by the human.")
        # Exact-forever grants are permanent — the request-expiry window only
        # bounds how long an *undecided* request stays actionable.
        if req.decision_mode != "approve_exact_forever" and (
            req.status == "expired" or req.expires_at <= datetime.utcnow()
        ):
            return _text(
                "Still pending — the human hasn't decided yet. Call "
                f'sutr__await_approval(request_id="{request_id}") again to '
                "keep waiting, or stop if they need more time."
            )

    # Reconstruct args from the stored normalized JSON so we call the upstream
    # tool with exactly what the human approved.
    try:
        arguments = json.loads(args_json) if args_json else {}
    except ValueError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    ctx = _pipeline_context(additional_info)

    # Consume the approve_once record (atomic state transition), or execute
    # under a standing approve-exact-forever grant. For allow_tool_forever /
    # auto_approved the agent should just call the tool directly.
    with Session(engine) as session:
        try:
            ensure_agent_can(session, _current_auth.get(), "tools:execute")
        except HTTPException:
            return _text("Your role does not allow executing tools (tools:execute).")

        req = session.get(ToolApprovalRequest, request_id)
        if req is not None and req.decision_mode == "approve_exact_forever":
            gate_log = find_gate_log(session, req.id)
            gate = GateResult(
                status="ready",
                args_hash=args_hash,
                access_reason="approved_exact",
                approval_request_id=req.id,
                pending_log_id=gate_log.id if gate_log else None,
            )
        else:
            consumed = try_consume_approved_request(
                session, org.id, integration_id, tool_name, args_hash
            )
            if consumed is None:
                # Either already consumed (double-wait race) or not of
                # decision_mode approve_once. Fall back to "share the decision"
                # rather than attempting to execute twice.
                return _text(
                    "The approval was recorded but can no longer be consumed here "
                    "(it may have been used already). Retry the original "
                    "sutr__call_tool call if you still need the tool to run."
                )
            gate_log = find_gate_log(session, consumed.id)
            gate = GateResult(
                status="ready",
                args_hash=args_hash,
                access_reason="approved_once",
                approval_request_id=consumed.id,
                pending_log_id=gate_log.id if gate_log else None,
            )

    outcome = await execute_tool(ctx, integration_id, tool_name, arguments, gate)
    return _outcome_to_contents(outcome)


# Stateful mode is required for server-pushed notifications like
# notifications/tools/list_changed — each client holds a persistent session
# with a dedicated SSE stream keyed by the mcp-session-id header. Idle
# sessions are reaped so dropped connections don't accumulate.
session_manager = StreamableHTTPSessionManager(
    app=mcp_server,
    stateless=False,
    session_idle_timeout=1800,
)
